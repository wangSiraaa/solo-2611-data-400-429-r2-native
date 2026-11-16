import json
from pathlib import Path
from engine import Engine
from batch_import import apply

case = json.loads(Path(__file__).with_name('batch-import-cases.json').read_text())


def start():
    engine = Engine()
    for row in case['initial']:
        engine.put(row['row_id'], row['category'], row['amount'])
    return engine


# 1) 两行交换分类：整批只增加一个 commit，旧/新快照的范围结果都精确。
engine = start()
before = engine.snapshot()
commit_after = apply(engine, case['swap'])
delta = engine.snapshot() - before
old_snap = engine.query(before, 10, 11, ['row_id', 'category', 'amount'])
new_snap = engine.query(engine.snapshot(), 10, 11, ['row_id', 'category', 'amount'])
assert delta == case['expected']['swap_commit_delta'] == 1
assert commit_after == engine.snapshot()
assert old_snap == [['a', 10, 100]]
assert new_snap == [['b', 10, 210]]

# 2) 同身份批内删除再重插：折叠为一个最终存活态，只增加一个 commit。
engine = start()
before = engine.snapshot()
apply(engine, case['same_identity'])
assert engine.snapshot() - before == case['expected']['same_identity_commit_delta'] == 1
assert engine.query(engine.snapshot(), 30, 31, ['row_id', 'amount']) == [['a', 300]]
assert engine.query(before, 30, 31, ['row_id', 'amount']) == []

# 3) 后半批非法：整批失败，行堆、索引、commit、已取得快照全部保持原状。
engine = start()
before = engine.snapshot()
fingerprint = engine.debug_state()
try:
    apply(engine, case['late_invalid'])
    raise AssertionError('invalid batch must fail')
except ValueError:
    pass
assert engine.snapshot() == before
assert engine.debug_state() == fingerprint
assert engine.query(before, 10, 11, ['row_id', 'amount']) == [['a', 100]]
assert engine.query(before, 20, 21, ['row_id', 'amount']) == [['b', 200]]

print(json.dumps({
    'swap_commit_delta': delta,
    'old_snapshot_range_10': old_snap,
    'new_snapshot_range_10': new_snap,
    'same_identity_commit_delta': 1,
    'late_invalid_state_unchanged': engine.debug_state() == fingerprint,
    'scope': 'Engine.apply 原子提交；失败批无任何可观察变化，快照发布边界与行堆/索引一致。',
}, ensure_ascii=False))
