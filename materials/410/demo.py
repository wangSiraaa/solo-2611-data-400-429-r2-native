import json
from engine import Engine

engine = Engine()
engine.put('a', 10, 100)
before = engine.snapshot()
normal = engine.query(before, 10, 11, ['row_id', 'amount'])

# 分类迁移 + 金额修改，之后再取新快照。
engine.put('a', 20, 200)
after = engine.snapshot()

# 旧快照：报表固定在迁移前，类别 10 必须仍能读到 a:100（旧实现此处漏行）。
old_view = engine.query(before, 10, 11, ['row_id', 'category', 'amount'])
# 同一 row_id 在新快照上类别 10 已不可见，类别 20 读到 a:200。
new_view_old_range = engine.query(after, 10, 11, ['row_id', 'category', 'amount'])
new_view_new_range = engine.query(after, 20, 21, ['row_id', 'category', 'amount'])

# 独立 oracle：只按 commit 不晚于快照逐行重放行堆，与二级索引无关。
def oracle(snapshot, lo, hi, projection):
    return [[r[col] for col in projection]
            for r in sorted(engine.heap.full_scan_at(snapshot),
                            key=lambda r: (r['category'], r['row_id'].encode()))
            if lo <= r['category'] < hi]

cases = [
    (before, 10, 11), (after, 10, 11), (after, 20, 21),
    (before, 0, 100), (after, 0, 100),
]
projection = ['row_id', 'category', 'amount']
all_match = all(engine.query(s, lo, hi, projection) == oracle(s, lo, hi, projection)
                for s, lo, hi in cases)

print(json.dumps(dict(
    normal_path_ok=normal == [['a', 100]],
    normal_observation='未修改时覆盖索引返回 a:100。',
    old_snapshot_view=old_view,
    new_snapshot_same_row=dict(old_range=new_view_old_range, new_range=new_view_new_range),
    snapshot_observation='同一 row_id：旧快照类别10读到 [a,10,100]，新快照类别10缺行、类别20读到 [a,20,200]。',
    counterexample_detected=not all_match,
    oracle_basis='仅按 commit 不晚于快照的逐行历史重放，独立于二级索引。',
    audit_old_snapshot=engine.audit,
), ensure_ascii=False))
