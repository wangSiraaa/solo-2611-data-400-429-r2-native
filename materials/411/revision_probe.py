"""复跑 savepoint-cases.json 的两个关键交错，并断言新系统行为。

旧版（legacy caller 手工回滚 writes）两人都能提交且无串行顺序；
现在通过正式 savepoint API 复现同一交错：回滚恢复私有写但不抹读取观察，
因此第二个提交必须 CONFLICT；非冲突路径中撤回的私有值不构成外部冲突。
"""

import json
from pathlib import Path

from storage import Store
from transaction import Transaction
from harness import run_file

case = json.loads(Path(__file__).with_name('savepoint-cases.json').read_text())

# ---- 冲突交错（与文件中 conflicting_schedule 同构，改为正式保存点 API）----
store = Store(case['initial'])
left, right = Transaction(store, label='T1'), Transaction(store, label='T2')
assert left.savepoint('s') == 'ACTIVE'
left_observation = left.scan(1, 1)
left.put(dict(row_id='b', team=1, on_call=False))
assert left.rollback_to('s') == 'ACTIVE'          # 撤回试写
assert left.writes == {}
right_observation = right.scan(1, 1)
right.put(dict(row_id='a', team=1, on_call=False))
right_result = right.commit()
left.put(dict(row_id='b', team=1, on_call=False))  # 仍依据早先观察
left_result = left.commit()
assert all(row['on_call'] for row in left_observation + right_observation)
assert (right_result, left_result) == ('COMMITTED', 'CONFLICT'), \
    (right_result, left_result)
assert left.conflict_evidence, 'CONFLICT 必须附带读取依据与致因变化'
# 仍有一人在岗，且失败事务的写没有泄漏
assert sum(r['on_call'] for r in store.head().values()) == 1
assert store.head()['b']['on_call'] is True

# ---- 非冲突交错：仅撤回私有写，原值得以保留并成功提交 ----
store2 = Store(case['initial'])
t1 = Transaction(store2, label='T1')
t1.savepoint('s')
t1.put(dict(row_id='a', team=1, on_call=False))
own_view = t1.scan(1, 1)
assert own_view[0]['on_call'] is False
t1.rollback_to('s')
assert t1.commit() == 'COMMITTED'
assert store2.head()['a']['on_call'] is True

# ---- 整个结构化用例文件（含串行顺序枚举核对）----
reports = run_file(Path(__file__).with_name('savepoint-cases.json'))
print(json.dumps({
    'anomaly_prevented': True,
    'conflicting_schedule': {'right': right_result, 'left': left_result,
                             'left_evidence': left.conflict_evidence},
    'nonconflicting_private_rollback_committed': True,
    'structured_runs_passed': [r['name'] for r in reports],
    'valid_serial_orders_per_run':
        {r['name']: r['valid_serial_orders'] for r in reports},
    'target_savepoint_api_present': all(
        hasattr(left, m) for m in ('savepoint', 'rollback_to', 'release')),
}, ensure_ascii=False, indent=2))
