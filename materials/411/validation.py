"""验证层。

职责按题目切分：
  * ``conflict``            —— 只做写写检测（保留原入口语义）；
  * ``predicate_conflict``  —— 谓词/点读取的重验（读记录在事务执行期形成，
                               这里拿提交点的已提交状态重放）。

串行化约定（本实现选定的串行验证顺序）：
  所有成功事务等价于“按实际 commit 先后、各自在提交瞬间紧前执行”的完整串行
  历史。因此第 N 个提交的事务必须证明：它返回给调用方的每一次 get/scan，
  用【当前已提交库 + 它自己当时覆盖的私有版本】重放时结果不变；并且它写过
  的键没有被更早提交的事务改过（first-committer-wins）。

关键：读记录带 provenance（snapshot 私有来源）。私有来源的观察只与该事务
自己的私有版本比较，撤销后的私有值绝不与外部当前值比较，因此保存点回滚
不会制造假冲突；snapshot 来源的观察才参与外部并发失效判断。
"""


class GetRead:
    """一次 get(row_id) 的读取依据。

    source='snapshot'：结果来自 begin 快照（可能为 None=缺失）；
    source='private' ：结果被本事务私有 put/delete 覆盖，wv 为私有版本号。
    wv_at_read：该读取发生时事务已产生的私有写版本水位，串行重放时只应用
    水位之内的私有写（读取之后的写不能影响更早的观察）。
    """

    __slots__ = ('key', 'source', 'wv', 'observed', 'wv_at_read')

    def __init__(self, key, source, wv, observed, wv_at_read):
        self.key = key
        self.source = source          # 'snapshot' | 'private'
        self.wv = wv                  # 私有版本号（private 时有效）
        self.observed = observed      # 返回给调用方的行副本或 None
        self.wv_at_read = wv_at_read

    def basis(self):
        if self.source == 'private':
            return f"get({self.key!r}) 依据本事务私有版本 wv={self.wv}"
        return f"get({self.key!r}) 依据 begin 快照，观察={_brief(self.observed)}"


class ScanRead:
    """一次 scan(low, high, on_call) 的完整谓词读取依据。

    matched   : 实际返回的行（row_id -> 行副本），含来源信息；
    in_range  : 谓词团队区间内、因 on_call 条件被排除的键及来源
                （on_call 翻转/迁入迁出必须被捕获）；
    provenance: 每个 in-range 键的来源 ('snapshot'/'private') 与私有版本号；
    observed  : 返回给调用方的有序 row_id 列表。
    """

    __slots__ = ('low', 'high', 'on_call', 'observed', 'matched',
                 'in_range', 'provenance', 'wv_at_read')

    def __init__(self, low, high, on_call, matched, in_range, provenance,
                 wv_at_read):
        self.low = low
        self.high = high
        self.on_call = on_call
        self.observed = sorted(matched)
        self.matched = matched
        self.in_range = in_range
        self.provenance = provenance
        self.wv_at_read = wv_at_read

    def basis(self):
        cond = '' if self.on_call is None else f', on_call={self.on_call}'
        return (f"scan(team {self.low}..{self.high}{cond}) 返回 "
                f"{self.observed}，区间内另存在但被条件排除的键="
                f"{sorted(self.in_range)}")


def _brief(row):
    return None if row is None else {k: row[k] for k in
                                     ('row_id', 'team', 'on_call') if k in row}


# ---------------------------------------------------------------- 写写检测
def conflict(store, tx):
    """仅检测本事务写过的行是否已被他人改过（原入口，返回冲突键列表）。"""
    return write_write_conflicts(store, tx)


def write_write_conflicts(store, tx):
    return [key for key in tx.writes
            if store.last_write.get(key, -1) > tx.start]


def write_write_evidence(store, tx, keys):
    """为写写冲突整理“造成失效的已提交变化”。"""
    out = []
    for key in keys:
        for rev in store.revisions_after(tx.start):
            delta = store.commits[rev]['delta']
            if key in delta:
                old, new = delta[key]
                out.append({
                    'kind': 'write_write',
                    'key': key,
                    'invalidated_by_revision': rev,
                    'owner': store.commits[rev]['owner'],
                    'committed_before': _brief(old),
                    'committed_after': _brief(new),
                })
    return out


# ---------------------------------------------------------------- 谓词重验
def _row_in_predicate(row, on_call):
    return row is not None and (on_call is None or row['on_call'] == on_call)


def predicate_conflict(store, tx):
    """重放事务全部已返回读取；无冲突返回 []，否则返回结构化失效证据。

    重放基线 = 提交点已提交库（store 头版本），再按每条读记录的 provenance
    叠加该事务所依据的私有版本（回滚后已不存在于 tx.writes 的版本由
    tx.private_versions 保存）。外部库对私有来源的键“不可见”，因此不会把
    撤销的私有值与外部当前值比较。
    """
    head = store.head()
    findings = []
    start_rows = store.snapshot(tx.start)

    for read in tx.reads:
        if isinstance(read, GetRead):
            findings += _recheck_get(store, tx, read, head, start_rows)
        else:
            findings += _recheck_scan(store, tx, read, head, start_rows)
    return findings


def _effective_row(tx, head, key, source, wv):
    """按来源取重放时该行的有效值。"""
    if source == 'private':
        version = tx.private_versions.get(wv)
        return None if version is None else version
    return head.get(key)


def _recheck_get(store, tx, read, head, start_rows):
    if read.source == 'private':
        # 私有来源：外部变化与该观察无关；私有版本内容不可变，恒可重放。
        return []
    current = head.get(read.key)
    if current == read.observed:
        return []
    return [_finding(store, tx, 'get', read.basis(), read.key,
                     start_rows.get(read.key), current,
                     _point_reason(read.observed, current))]


def _point_reason(observed, current):
    if observed is None:
        return 'phantom_insert: 原缺失键被他人插入'
    if current is None:
        return 'deleted: get 已返回的行被他人删除'
    if current['team'] != observed['team']:
        return 'team_moved: 已返回行的 team 被改变'
    return 'value_changed: 已返回行的 on_call 被改变'


def _recheck_scan(store, tx, read, head, start_rows):
    findings = []
    # 重放：begin 时在团队区间内的键、头版本落在区间内的键、以及读取记录里
    # 留痕的私有删除/移出键，三者并集保证“空范围出现新行 / 迁入 / 迁出 /
    # 私有墓碑”都不会漏；按来源取有效值。
    candidate_keys = set(read.in_range) | set(read.matched) | \
        set(read.provenance)
    for key, row in head.items():
        if read.low <= row['team'] <= read.high:
            candidate_keys.add(key)

    replayed_matched = {}
    replayed_in_range = {}
    for key in candidate_keys:
        source, wv = read.provenance.get(key, ('snapshot', None))
        row = _effective_row(tx, head, key, source, wv)
        if row is not None and read.low <= row['team'] <= read.high:
            if _row_in_predicate(row, read.on_call):
                replayed_matched[key] = row
            else:
                replayed_in_range[key] = row

    same_matched = sorted(replayed_matched) == read.observed and \
        all(_same_row(replayed_matched[k], read.matched[k])
            for k in read.observed)
    same_in_range = sorted(replayed_in_range) == sorted(read.in_range) and \
        all(_same_row(replayed_in_range[k], read.in_range[k])
            for k in read.in_range)
    if same_matched and same_in_range:
        return findings

    # 找出具体失效键与依据，只归因 snapshot 来源（私有来源恒可重放）。
    old_keys = set(read.matched) | set(read.in_range)
    new_keys = set(replayed_matched) | set(replayed_in_range)
    for key in old_keys | new_keys:
        source, _wv = read.provenance.get(key, ('snapshot', None))
        if source == 'private':
            continue
        before = start_rows.get(key)
        after = head.get(key)
        if before == after:
            continue
        was_match = key in read.matched
        now_row = after
        now_match = (now_row is not None
                     and read.low <= now_row['team'] <= read.high
                     and _row_in_predicate(now_row, read.on_call))
        reason = _scan_reason(before, after, was_match, now_match)
        findings.append(_finding(store, tx, 'scan', read.basis(), key,
                                 before, after, reason))
    if not findings:
        # 安全网：谓词结果确实变了却无法归因到单个键时，保守中止该事务，
        # 绝不静默放行（宁可保守中止，不可破坏可串行化）。
        findings.append({
            'kind': 'scan',
            'reason': 'predicate_unreproducible: 谓词结果无法在提交点重放（保守中止）',
            'key': None,
            'read_basis': read.basis(),
            'observed_before': sorted(read.observed),
            'committed_now': sorted(replayed_matched),
            'invalidated_by': [
                {'revision': r, 'owner': store.commits[r]['owner']}
                for r in store.revisions_after(tx.start)],
        })
    return findings


def _same_row(a, b):
    return a == b


def _scan_reason(before, after, was_match, now_match):
    if before is None and after is not None:
        return 'phantom_insert: 他人向已读取范围插入原来为空的键'
    if before is not None and after is None:
        return 'deleted: 范围内已观察的行被他人删除'
    b_team, a_team = before['team'], after['team']
    if b_team != a_team:
        if was_match and not now_match:
            return 'predicate_move_out: 行被他人改 team 移出谓词范围'
        if not was_match and now_match:
            return 'predicate_move_in: 行被他人改 team 移入谓词范围'
    return 'value_changed: 已返回行的 on_call 等被他人改变'


def _finding(store, tx, read_kind, read_basis, key, before, after, reason):
    invalidators = []
    for rev in store.revisions_after(tx.start):
        delta = store.commits[rev]['delta']
        if key in delta:
            old, new = delta[key]
            invalidators.append({
                'revision': rev,
                'owner': store.commits[rev]['owner'],
                'before': _brief(old),
                'after': _brief(new),
            })
    return {
        'kind': read_kind,
        'reason': reason,
        'key': key,
        'read_basis': read_basis,
        'observed_before': _brief(before),
        'committed_now': _brief(after),
        'invalidated_by': invalidators,
    }
