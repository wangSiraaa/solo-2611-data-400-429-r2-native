"""跨 run 的全局可见性读取。

可见性规则（逻辑历史与 run 切分无关）：
  * 同一 key 只看所有 put 中 seq 最大者（winner）；
  * 该 winner 可见，当且仅当它的 seq 严格大于覆盖该 key 的所有 erase 的 seq；
  * erase 作用于半开区间 lo <= key < hi，较新的 put 可以在删除后重新出现。

实现采用扫描线：把 erase 看作 [lo, hi) 上的事件，在每个出现过点记录的 key
处，用最小堆维护当前覆盖该 key 的区间的最大 seq。整个过程只遍历实际记录和
区间边界，复杂度 O((P + E) log E)，不枚举区间内任何整数。
"""
import bisect
import heapq


def _collect(store):
    """收集全库的点记录（按 key 分组）与全部 erase。"""
    points_by_key = {}
    erases = []
    for run_id in store.ids():
        for rec in store.read(run_id):
            if rec['kind'] == 'put':
                old = points_by_key.get(rec['key'])
                if old is None or rec['seq'] > old['seq']:
                    points_by_key[rec['key']] = rec
            else:
                erases.append(rec)
    return points_by_key, erases


def visible_map(store):
    """返回 {key: 可见的点记录}，只含可见键。"""
    points_by_key, erases = _collect(store)
    if not points_by_key:
        return {}
    cover = _sweep_cover_max(erases, sorted(points_by_key))
    visible = {}
    for key, point in points_by_key.items():
        if point['seq'] > cover.get(key, 0):
            visible[key] = point
    return visible


def _sweep_cover_max(erases, sorted_keys):
    """扫描线：在 sorted_keys 每个位置求覆盖 erase 的最大 seq。

    用 (hi, seq) 结束堆做惰性到期清理，避免按 seq 反查 hi。
    """
    if not sorted_keys:
        return {}
    events = []  # (位置, 类型, seq/hi)
    for r in erases:
        events.append((r['lo'], 0, r['hi'], r['seq']))  # 开始
    events.sort(key=lambda e: (e[0], e[1]))
    open_heap = []   # (-seq, hi)
    answer = {}
    ei = 0
    for key in sorted_keys:
        while ei < len(events) and events[ei][0] <= key:
            _, _, hi, seq = events[ei]
            heapq.heappush(open_heap, (-seq, hi))
            ei += 1
        while open_heap and open_heap[0][1] <= key:  # 半开区间：hi 处失效
            heapq.heappop(open_heap)
        answer[key] = -open_heap[0][0] if open_heap else 0
    return answer


def sweep_work(store):
    """计量版扫描：返回 (visible_map, 统计)。

    统计真实发生的工作：点比较次数、erase 事件数、堆 push/pop 次数、
    可见性判定次数。用于设计说明里的代价展示（见 cost_report.py）。
    """
    points_by_key, erases = _collect(store)
    stats = {'runs': len(store.ids()), 'point_records_seen': 0,
             'winner_comparisons': 0, 'erase_records': len(erases),
             'heap_push': 0, 'heap_pop': 0, 'visibility_checks': 0}
    for run_id in store.ids():
        for rec in store.read(run_id):
            if rec['kind'] == 'put':
                stats['point_records_seen'] += 1
                old = points_by_key.get(rec['key'])
                if old is not None:
                    stats['winner_comparisons'] += 1
    cover = {}
    if points_by_key:
        events = sorted((r['lo'], r['hi'], r['seq']) for r in erases)
        open_heap = []
        ei = 0
        for key in sorted(points_by_key):
            while ei < len(events) and events[ei][0] <= key:
                _, hi, seq = events[ei]
                heapq.heappush(open_heap, (-seq, hi))
                stats['heap_push'] += 1
                ei += 1
            while open_heap and open_heap[0][1] <= key:
                heapq.heappop(open_heap)
                stats['heap_pop'] += 1
            cover[key] = -open_heap[0][0] if open_heap else 0
    visible = {}
    for key, point in points_by_key.items():
        stats['visibility_checks'] += 1
        if point['seq'] > cover.get(key, 0):
            visible[key] = point
    return visible, stats


def scan(store, lo, hi):
    """按整数键序返回半开 [lo, hi) 内全部可见 [key, value]。"""
    if not (isinstance(lo, int) and isinstance(hi, int)) or lo >= hi:
        raise ValueError('scan requires lo < hi integer bounds')
    visible = visible_map(store)
    keys = sorted(visible)
    left = bisect.bisect_left(keys, lo)
    right = bisect.bisect_left(keys, hi)
    return [[k, visible[k]['value']] for k in keys[left:right]]


def get(store, key):
    """返回 key 的可见值，不存在返回 None。

    点查走“刺点”路径：只找该 key 的 winner 与覆盖该 key 的 erase，
    不构造全库扫描结构，工作量 O(P + E)（受 1000 条物理记录上限约束）。
    """
    if not isinstance(key, int):
        raise ValueError('key must be integer')
    winner = None
    for rec in store.all_records():
        if rec['kind'] == 'put' and rec['key'] == key:
            if winner is None or rec['seq'] > winner['seq']:
                winner = rec
    if winner is None:
        return None
    cover_max = 0
    for rec in store.all_records():
        if rec['kind'] == 'erase' and rec['lo'] <= key < rec['hi']:
            if rec['seq'] > cover_max:
                cover_max = rec['seq']
    return winner['value'] if winner['seq'] > cover_max else None
