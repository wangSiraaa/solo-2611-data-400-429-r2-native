import json
from engine import Engine

engine = Engine()
engine.put('a', 10, 100)
before = engine.snapshot()
normal = engine.query(before, 10, 11, ['row_id', 'amount'])
engine.put('a', 20, 200)
actual = engine.query(before, 10, 11, ['row_id', 'amount'])
expected = [[r['row_id'], r['amount']] for r in engine.heap.full_scan_at(before)
            if 10 <= r['category'] < 11]
print(json.dumps(dict(normal_path_ok=normal == [['a',100]],
    normal_observation='未修改时覆盖索引返回a:100。',
    counterexample_detected=actual != expected, actual=actual, expected=expected,
    failure_observation='分类由10迁到20后，旧快照查询10返回空，oracle仍为a:100。',
    oracle_basis='仅按commit不晚于快照的逐行历史重放，独立于二级索引。'), ensure_ascii=False))
