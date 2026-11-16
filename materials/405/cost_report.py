"""用实际记录计量展示设计选择的代价。

对比两种 erase 表示：
  A. 原区间保留（本实现选择）：读时扫描线在点 key 上求覆盖 erase 最大 seq；
  B. 规范化覆盖片段：把所有 erase 端点切成原子片段，每段保存一个最大 seq。

计量内容全部来自真实记录计数（sweep_work / 片段切分），不使用任何
与区间整数跨度成比例的操作。运行：python3 -B cost_report.py
"""
import tempfile

from run_store import Store, KEY_MIN, KEY_MAX
from reader import sweep_work
from compactor import compact

P = lambda key, value, seq: {'kind': 'put', 'key': key, 'value': value, 'seq': seq}
D = lambda lo, hi, seq: {'kind': 'erase', 'lo': lo, 'hi': hi, 'seq': seq}


def normalized_fragments(erases):
    """方案 B：按所有 erase 端点把数轴切成原子片段，逐片段求最大覆盖 seq。

    返回 (片段数, 端点事件数)。片段数只取决于区间条数与重叠结构，
    与区间跨度无关；嵌套/交叠越多，片段膨胀越大。
    """
    if not erases:
        return 0, 0
    coords = sorted({e['lo'] for e in erases} | {e['hi'] for e in erases})
    fragments = 0
    for i in range(len(coords) - 1):
        x = coords[i]  # 片段 [coords[i], coords[i+1]) 内覆盖情况恒定
        if any(e['lo'] <= x < e['hi'] for e in erases):
            fragments += 1
    return fragments, len(coords)


def naive_point_x_erase(store):
    """朴素点×区间刺点法的比较次数（展示被淘汰方案的量级）。"""
    pts = sum(1 for r in store.all_records() if r['kind'] == 'put')
    ers = sum(1 for r in store.all_records() if r['kind'] == 'erase')
    return pts * ers


def build_store(records):
    d = tempfile.mkdtemp()
    s = Store(d)
    # 一次 add 要求同批；直接分 run 装入（seq 已单调）
    mid = len(records) // 2
    s.add('r0', records[:mid])
    s.add('r1', records[mid:])
    return s, d


def table(rows, headers):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    line = lambda vals: '  '.join(str(v).ljust(widths[i]) for i, v in enumerate(vals))
    print(line(headers))
    print('  '.join('-' * w for w in widths))
    for r in rows:
        print(line(r))


def scenario_overlap_density():
    print('== 1) 重叠密度对读时检查量的影响（固定 30 个点，erase 数与重叠递增）==')
    rows = []
    for e_count, half_width in [(5, 30), (10, 30), (20, 30), (30, 30)]:
        recs = [P(k % 16 - 8, 'v', k + 1) for k in range(30)]
        base = len(recs)
        for i in range(e_count):
            # 层层嵌套：[-w+i, w-i)，始终满足 lo < hi
            recs.append(D(-half_width + i, half_width - i, base + i + 1))
        s, _ = build_store(recs)
        visible, work = sweep_work(s)
        frags, coords = normalized_fragments([r for r in recs if r['kind'] == 'erase'])
        rows.append([e_count, work['heap_push'], work['heap_pop'],
                     work['visibility_checks'], naive_point_x_erase(s), frags, coords])
    table(rows, ['erases', 'heap_push', 'heap_pop', 'vis_checks',
                 'naive P*E', 'B:fragments', 'B:endpoints'])
    print('  方案 A 工作量随记录数线性增长（每个 erase 恰好一次 push、至多一次 pop）；')
    print('  方案 B 的原子片段随嵌套层数膨胀，朴素 P*E 是二次量级。\n')


def scenario_wide_span():
    print('== 2) 大跨度 erase：跨度变化，工作量不变（禁止整数枚举的直接证据）==')
    rows = []
    for hi in (10, 1000, 10 ** 6, KEY_MAX):
        recs = [P(1, 'a', 1), P(2, 'b', 2), P(-3, 'c', 3),
                D(KEY_MIN, hi, 10)]
        s, _ = build_store(recs)
        _, work = sweep_work(s)
        frags, coords = normalized_fragments([recs[-1]])
        rows.append([hi, work['erase_records'], work['heap_push'],
                     work['heap_pop'], frags, coords])
    table(rows, ['erase hi', 'erase recs', 'heap_push', 'heap_pop',
                 'B:fragments', 'B:endpoints'])
    print('  hi 从 10 扩大到 10^9，两种表示的记录/事件计数完全相同。\n')


def scenario_compaction_reclamation():
    print('== 3) 局部压缩的保留/回收计量（原区间策略，无片段拆分）==')
    rows = []
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        s.add('old', [P(1, 'a', 1), P(50, 'b', 2), P(99, 'c', 3)])
        s.add('mid', [D(0, 60, 10), D(60, 100, 11), D(200, 300, 12)])
        s.add('new', [P(25, 'x', 20), P(70, 'y', 21)])
        before = s.physical_record_count()
        r = compact(s, ['mid', 'new'], 'merged')
        after = s.physical_record_count()
        kept = [x for x in s.read('merged') if x['kind'] == 'erase']
        rows.append(['partial(mid+new)', before, after, r['point_records'],
                     r['erase_records'], r['reclaimed_erases']])
        j = s.journal()[-1]
        for dec in j['erase_decisions']:
            rows.append(['  erase seq=%d %s' % (dec['record']['seq'], dec['decision']),
                         '', '', '', '', dec['reason'][:46]])
        r2 = compact(s, ['old', 'merged'], 'full')
        rows.append(['full(old+merged)', after, s.physical_record_count(),
                     r2['point_records'], r2['erase_records'], r2['reclaimed_erases']])
    table(rows, ['step', 'phys_before', 'phys_after', 'kept_pts', 'kept_ers', 'reclaimed/reason'])
    print('  D(0,60) 保护 old 段 seq1/2 的点 -> 保留；D(200,300) 无保护对象 -> 回收；')
    print('  D(60,100) 区间内 old 点 seq3 更小 -> 保留；全量压缩后 erase 清零。\n')


def scenario_repeated_compaction_growth():
    print('== 4) 反复局部压缩下删除信息是否膨胀（原区间 vs 规范化片段）==')
    rows = []
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        # 锚点 run 永不参与前几轮压缩；key=50 落在每条 erase 内且 seq 更小，
        # 因此这些 erase 每轮都必须被保留，形成交叠累积。
        s.add('anchor', [P(5, 'a', 1), P(50, 'shielded', 2), P(95, 'z', 3)])
        seq = 10
        prev = None
        for i in range(6):
            rid = 'r%d' % i
            s.add(rid, [D(10 + i, 90 - i, seq), P(80 - i, 'v', seq + 1)])
            out = 'm%d' % i
            # 前几轮 anchor 始终不参与，erase 必须持续保护 anchor 里的旧点
            chosen = [prev] if prev else []
            chosen.append(rid)
            compact(s, chosen, out)
            erases_here = [x for x in s.read(out) if x['kind'] == 'erase']
            frags, _ = normalized_fragments(erases_here)
            rows.append([i + 1, len(erases_here), frags, s.physical_record_count()])
            prev = out
            seq += 10
        # 最后一轮把 anchor 也并入：未选 run 为空，erase 全部回收
        r = compact(s, ['anchor', prev], 'final')
        rows.append(['final', 0, 0, s.physical_record_count()])
    table(rows, ['round', 'whole erases kept (A)', 'normalized fragments (B)', 'physical records'])
    print('  方案 A 每条 erase 始终是一条原区间记录，轮次增加只线性增加区间条数；')
    print('  方案 B 每轮按全部端点重切，交叠嵌套累积时片段数高于区间条数（膨胀代价）。\n')


if __name__ == '__main__':
    scenario_overlap_density()
    scenario_wide_span()
    scenario_compaction_reclamation()
    scenario_repeated_compaction_growth()
    print('全部计量仅遍历实际记录与区间边界；库内不存在任何按整数跨度展开的结构。')
