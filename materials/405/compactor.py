"""安全局部压缩。

compact(store, selected, output_id) 把任意非空 run 子集替换成一个新 run，
压缩前后所有 get/scan 结果必须完全相同。关键不变量：

1. 点版本只做“全局视角”的淘汰：选中 run 里的某个 put 被丢弃，当且仅当它
   不是该 key 的全库 winner，或者它虽是 winner 但被某条 seq 更大的 erase
   遮盖（压缩后本就不可见）。选中 run 内部先过滤再合并的旧做法会让未选
   run 的旧值复活，这里禁止使用。
2. erase 不能因为“选中输入里的点已经被它删掉”就丢弃：它可能还在保护
   未选 run 中 seq 更小的点。保守回收规则（题面允许的整条保留策略）：
   选中 run 的一条 erase 保留原区间，当且仅当未选 run 中存在至少一个点
   p，满足 p.seq < erase.seq 且 lo <= p.key < hi；否则整条回收。
   未选 run 自己的 erase 原样留在未选 run 中，不参与本次决策。
3. 全量压缩（选中全部 run）时未选 run 为空，任何 erase 都没有保护对象，
   输出只含当前可见点、不含任何删除记录。
4. 撤下的旧 run 文件由 store.replace 删除，不能继续参与查询。

每次决策写入 store 的 journal，inspect 可以复查每条保留/回收的依据。
"""
from reader import _sweep_cover_max  # 同包：复用扫描线，不枚举整数


def _gather(store, selected):
    sel_points, sel_erases, out_points, out_erases = [], [], [], []
    selected_set = set(selected)
    for run_id in store.ids():
        bucket_records = store.read(run_id)
        target_points = sel_points if run_id in selected_set else out_points
        target_erases = sel_erases if run_id in selected_set else out_erases
        for rec in bucket_records:
            if rec['kind'] == 'put':
                target_points.append(dict(rec, run=run_id))
            else:
                target_erases.append(dict(rec, run=run_id))
    return sel_points, sel_erases, out_points, out_erases


def _winners(points):
    win = {}
    for p in points:
        old = win.get(p['key'])
        if old is None or p['seq'] > old['seq']:
            win[p['key']] = p
    return win


def _has_protector_point(unselected_points, lo, hi, seq):
    """未选 run 中是否存在被该 erase 保护的旧点（落在原区间且 seq 更小）。

    只遍历实际存在的点记录并做边界比较，不枚举 [lo, hi) 内整数；
    库内物理记录上限 1000，线性扫描即足够，witness 仅留前 5 条供 inspect。
    """
    witnesses = []
    for p in unselected_points:
        if lo <= p['key'] < hi and p['seq'] < seq:
            if len(witnesses) < 5:
                witnesses.append({'run': p['run'], 'key': p['key'], 'seq': p['seq']})
    return bool(witnesses), witnesses


def compact(store, selected, output_id):
    ids = store.ids()
    selected_set = set(selected)
    if not selected_set or not selected_set <= set(ids):
        raise ValueError('selected must be a non-empty subset of active runs')

    sel_points, sel_erases, out_points, out_erases = _gather(store, selected)

    # 全库 winner（跨 run，同一 key 取 seq 最大者）
    global_winners = _winners(sel_points + out_points)

    # 每个 winner key 上覆盖 erase 的最大 seq（扫描线，只走边界）
    all_erases = sel_erases + out_erases
    cover = _sweep_cover_max(all_erases, sorted(global_winners)) if global_winners else {}

    # ---- 输出点：只保留选中 run 中仍可见的全局 winner ----
    kept_points, point_decisions = [], []
    dropped = {'overwritten_version': 0, 'deleted_visible': 0}
    for p in sorted(sel_points, key=lambda r: (r['key'], r['seq'])):
        winner = global_winners[p['key']]
        if winner['seq'] != p['seq']:
            dropped['overwritten_version'] += 1
            point_decisions.append({
                'record': {'kind': 'put', 'key': p['key'], 'value': p['value'], 'seq': p['seq']},
                'run': p['run'], 'decision': 'drop',
                'reason': 'overwritten by newer put seq=%d (run=%s)'
                          % (winner['seq'], winner['run'])})
            continue
        cover_max = cover.get(p['key'], 0)
        if p['seq'] <= cover_max:
            dropped['deleted_visible'] += 1
            point_decisions.append({
                'record': {'kind': 'put', 'key': p['key'], 'value': p['value'], 'seq': p['seq']},
                'run': p['run'], 'decision': 'drop',
                'reason': 'hidden by covering erase seq=%d' % cover_max})
            continue
        kept_points.append({'kind': 'put', 'key': p['key'],
                            'value': p['value'], 'seq': p['seq']})
        point_decisions.append({
            'record': {'kind': 'put', 'key': p['key'], 'value': p['value'], 'seq': p['seq']},
            'run': p['run'], 'decision': 'keep',
            'reason': 'global winner and visible (covering erase max seq=%d)' % cover_max})

    # ---- 输出 erase：只保留选中 run 中仍有保护对象的原区间 ----
    kept_erases, erase_decisions = [], []
    reclaimed = 0
    for e in sorted(sel_erases, key=lambda r: r['seq']):
        protected, witnesses = _has_protector_point(
            out_points, e['lo'], e['hi'], e['seq'])
        base = {'kind': 'erase', 'lo': e['lo'], 'hi': e['hi'], 'seq': e['seq']}
        if protected:
            kept_erases.append(base)
            erase_decisions.append({
                'record': base, 'run': e['run'], 'decision': 'keep',
                'reason': 'still protects older points in unselected runs',
                'witness_points': witnesses})
        else:
            reclaimed += 1
            erase_decisions.append({
                'record': base, 'run': e['run'], 'decision': 'reclaim',
                'reason': 'no point with smaller seq in [%d, %d) inside any unselected run'
                          % (e['lo'], e['hi'])})

    output_records = sorted(kept_points, key=lambda r: r['key']) + \
        sorted(kept_erases, key=lambda r: r['seq'])

    # 未选 run 的 erase 不在输出里，但仍在各自 run 中有效；记录数量供检查
    untouched_erases = len(out_erases)

    journal_entry = {
        'output': output_id,
        'selected': sorted(selected_set),
        'unselected_runs': [r for r in ids if r not in selected_set],
        'kept_points': len(kept_points),
        'dropped_points': dropped,
        'kept_erases': len(kept_erases),
        'reclaimed_erases': reclaimed,
        'erases_untouched_in_unselected_runs': untouched_erases,
        'point_decisions': point_decisions,
        'erase_decisions': erase_decisions,
        'policy': 'keep whole original erase interval iff it still shields an '
                  'older point in an unselected run',
    }
    receipt = {
        'selected': sorted(selected_set),
        'output': output_id,
        'point_records': len(kept_points),
        'erase_records': len(kept_erases),
        'reclaimed_erases': reclaimed,
        'dropped_point_versions': dropped['overwritten_version'] + dropped['deleted_visible'],
    }
    store.replace(selected, output_id, output_records, journal_entry=journal_entry)
    return receipt
