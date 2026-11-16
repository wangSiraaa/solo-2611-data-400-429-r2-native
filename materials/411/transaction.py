"""本地乐观关系事务。

  * begin（构造）取当前快照 revision，之后 get/scan 只碰不可变快照与本事务
    私有字典，绝不持有全局互斥锁；
  * get/scan 叠加本事务私有 put/delete，并把每次【已返回给调用方】的读取
    连同 provenance 记入 reads —— 保存点回滚只恢复私有写，不撤回读取观察；
  * scan 记录谓词团队闭区间内的存在、缺失与值（含被 on_call 条件排除的键），
    验证由 validation.predicate_conflict 在提交点重放；
  * savepoint/rollback_to/release 最多 4 个嵌套保存点，名称在活跃保存点中
    唯一；保存点不动 begin 快照，也不向其他事务发布任何数据；
  * commit 先写写检测、再谓词重验，全部通过才整批 publish，否则 CONFLICT
    且源不变；终态事务重复查询/重复提交只返回第一次结果。
"""

from validation import (GetRead, ScanRead, predicate_conflict,
                        write_write_conflicts, write_write_evidence)

ACTIVE = 'ACTIVE'
COMMITTED = 'COMMITTED'
CONFLICT = 'CONFLICT'
ABORTED = 'ABORTED'
INVALID_SAVEPOINT = 'INVALID_SAVEPOINT'

_MAX_SAVEPOINTS = 4


class Transaction:
    _auto = 0

    def __init__(self, store, label=None):
        self.store = store
        Transaction._auto += 1
        self.label = label or f'T{Transaction._auto}'
        # begin：只取当前快照版本，无锁
        self.start = store.begin()
        # 私有写：key -> 行或 None(删除墓碑)
        self.writes = {}
        # 私有版本存档：wv -> 当时写入的行/None，回滚后仍保留，供读取重放
        self.private_versions = {}
        self._wv_counter = 0
        # 当前私有写键 -> 私有版本号
        self._key_wv = {}
        # 已返回读取（GetRead/ScanRead）；回滚保存点不清除
        self.reads = []
        # 保存点栈：[{'name','writes','key_wv'}]
        self._savepoints = []
        self.state = ACTIVE
        # 终态证据与终态读缓存
        self.conflict_evidence = []
        self._terminal_read_cache = {}

    # ------------------------------------------------------------- 私有视图
    def _merged_view(self):
        """begin 快照叠加当前私有写，返回完整行字典（私有副本）。"""
        rows = self.store.snapshot(self.start)
        for key, value in self.writes.items():
            if value is None:
                rows.pop(key, None)
            else:
                rows[key] = value.copy()
        return rows

    def _next_wv(self):
        self._wv_counter += 1
        return self._wv_counter

    # ------------------------------------------------------------------ 读
    def get(self, row_id):
        """点读：叠加私有 put/delete；返回行副本或 None。"""
        if self.state != ACTIVE:
            return self._frozen_get(row_id)
        if row_id in self.writes:
            wv = self._key_wv[row_id]
            value = self.writes[row_id]
            observed = None if value is None else value.copy()
            self.reads.append(GetRead(row_id, 'private', wv, observed,
                                      self._wv_counter))
            return None if observed is None else observed.copy()
        observed = self.store.history[self.start].get(row_id)
        self.reads.append(GetRead(row_id, 'snapshot', None,
                                  None if observed is None else observed.copy(),
                                  self._wv_counter))
        return None if observed is None else observed.copy()

    def scan(self, low, high, on_call=None):
        """谓词扫描：team 闭区间 + 可选 on_call，按 row_id 排序返回完整行。"""
        if self.state != ACTIVE:
            return self._frozen_scan(low, high, on_call)
        snapshot = self.store.history[self.start]
        matched, in_range, provenance = {}, {}, {}
        # 候选键 = 快照中团队在区间内的键 ∪ 私有写键。私有删除/移出的键不
        # 出现在结果里，但 provenance 必须留痕，否则验证时会被当成外部行。
        candidate = set()
        for key, row in snapshot.items():
            if low <= row['team'] <= high:
                candidate.add(key)
        for key, value in self.writes.items():
            if value is not None and low <= value['team'] <= high:
                candidate.add(key)
            elif key in snapshot and low <= snapshot[key]['team'] <= high:
                candidate.add(key)  # 私有删除/移出：仍需记录来源
        rows = self._merged_view()
        for key in sorted(candidate):
            if key in self.writes:
                source, wv = 'private', self._key_wv[key]
            else:
                source, wv = 'snapshot', None
            provenance[key] = (source, wv)
            row = rows.get(key)  # 私有墓碑 -> None：记录来源但不进结果
            if row is None:
                continue
            if not (low <= row['team'] <= high):
                continue  # 私有版本已移出团队区间
            if on_call is None or row['on_call'] == on_call:
                matched[key] = row.copy()
            else:
                in_range[key] = row.copy()
        self.reads.append(ScanRead(low, high, on_call, matched, in_range,
                                   dict(provenance), self._wv_counter))
        return [matched[k].copy() for k in sorted(matched)]

    # --------------------------------------------------------------- 私有写
    def put(self, row):
        if self.state != ACTIVE:
            return self.state
        row = dict(row)
        wv = self._next_wv()
        self.writes[row['row_id']] = row
        self._key_wv[row['row_id']] = wv
        self.private_versions[wv] = row.copy()
        return ACTIVE

    def delete(self, row_id):
        if self.state != ACTIVE:
            return self.state
        wv = self._next_wv()
        self.writes[row_id] = None
        self._key_wv[row_id] = wv
        self.private_versions[wv] = None
        return ACTIVE

    # -------------------------------------------------------------- 保存点
    def savepoint(self, name):
        if self.state != ACTIVE:
            return INVALID_SAVEPOINT
        if not isinstance(name, str) or name in self._active_names():
            return INVALID_SAVEPOINT
        if len(self._savepoints) >= _MAX_SAVEPOINTS:
            return INVALID_SAVEPOINT
        self._savepoints.append({
            'name': name,
            'writes': dict(self.writes),
            'key_wv': dict(self._key_wv),
        })
        return ACTIVE

    def rollback_to(self, name):
        """恢复该点的私有写状态并移除其后的保存点；目标点保留、可再回滚。"""
        if self.state != ACTIVE:
            return INVALID_SAVEPOINT
        idx = self._index_of(name)
        if idx is None:
            return INVALID_SAVEPOINT
        sp = self._savepoints[idx]
        self.writes = dict(sp['writes'])
        self._key_wv = dict(sp['key_wv'])
        # 私有版本号保持单调递增、版本内容永不剪枝：更早的读取可能仍引用被
        # 撤销版本（回滚不撤回读取观察），验证器要按“当时由自己的哪个私有
        # 版本覆盖”重放；回滚后新的 put 会拿到全新版本号。
        # 移除其后保存点，目标点仍在
        self._savepoints = self._savepoints[:idx + 1]
        # 注意：reads 不动——已返回的读取观察仍须参加提交验证
        return ACTIVE

    def release(self, name):
        """移除该点及其内层保存点，私有写全部保留。"""
        if self.state != ACTIVE:
            return INVALID_SAVEPOINT
        idx = self._index_of(name)
        if idx is None:
            return INVALID_SAVEPOINT
        del self._savepoints[idx:]
        return ACTIVE

    def _active_names(self):
        return {sp['name'] for sp in self._savepoints}

    def _index_of(self, name):
        for i, sp in enumerate(self._savepoints):
            if sp['name'] == name:
                return i
        return None

    # ------------------------------------------------------------- 事务终态
    def commit(self):
        if self.state != ACTIVE:
            return self.state  # 重复提交返回第一次结果
        ww = write_write_conflicts(self.store, self)
        if ww:
            return self._fail(write_write_evidence(self.store, self, ww))
        pred = predicate_conflict(self.store, self)
        if pred:
            return self._fail(pred)
        # 全部验证通过：整批发布（publish 内部原子，无半批）
        rev = self.store.publish(self.writes, owner=self.label)
        self.committed_revision = rev
        self.state = COMMITTED
        self._freeze()
        return self.state

    def abort(self):
        if self.state != ACTIVE:
            return self.state
        self.state = ABORTED
        # 丢弃私有状态；读取与证据保留供审计，但绝不发布
        self.writes = {}
        self._key_wv = {}
        self._savepoints = []
        self._freeze()
        return self.state

    def _fail(self, evidence):
        self.state = CONFLICT
        self.conflict_evidence = evidence
        # 源不变：不调用 publish，私有写不发布
        self._freeze()
        return self.state

    def _freeze(self):
        """终态：冻结读视图，之后重复查询返回第一次结果，不新增读记录。"""
        view = self._merged_view()
        self._terminal_read_cache['view'] = view

    def _frozen_get(self, row_id):
        view = self._terminal_read_cache['view']
        row = view.get(row_id)
        return None if row is None else row.copy()

    def _frozen_scan(self, low, high, on_call):
        view = self._terminal_read_cache['view']
        out = []
        for key in sorted(view):
            row = view[key]
            if low <= row['team'] <= high and \
                    (on_call is None or row['on_call'] == on_call):
                out.append(row.copy())
        return out

    def read_basis(self):
        """审计用：列出本事务全部已返回读取的依据。"""
        return [r.basis() for r in self.reads]
