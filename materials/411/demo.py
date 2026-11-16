"""真实演示：正常提交、write-skew 拦截（含读取依据与致因已提交变化）、
谓词幻影、无关 team 并行成功，以及保存点回滚不抹读取观察。

直接运行：python3 demo.py
所有结果由事务系统当场计算，没有任何预写调度结果。
"""

import json

from storage import Store
from transaction import Transaction


def show(title, payload):
    print(f'== {title} ==')
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()


# 1) 正常路径：独立新行事务成功提交，源含三行
seed = [dict(row_id='A', team=1, on_call=True),
        dict(row_id='B', team=1, on_call=True)]
normal_store = Store(seed)
normal_tx = Transaction(normal_store, label='normal')
normal_tx.put(dict(row_id='C', team=2, on_call=True))
normal_state = normal_tx.commit()
show('normal_commit', {
    'state': normal_state,
    'revision': normal_store.revision,
    'final_rows': sorted(normal_store.head()),
    'ok': normal_state == 'COMMITTED' and len(normal_store.head()) == 3,
})

# 2) write-skew：两人都看到“还有另一人在岗”后分别离岗
store = Store([dict(row) for row in seed])
a, b = Transaction(store, label='A'), Transaction(store, label='B')
a_scan, b_scan = a.scan(1, 1, True), b.scan(1, 1, True)
for tx, rid in ((a, 'A'), (b, 'B')):
    # 业务规则：扫描到至少 2 人在岗才允许自己离岗
    if len(tx.scan(1, 1, True)) > 1:
        tx.put(dict(row_id=rid, team=1, on_call=False))
states = [a.commit(), b.commit()]
show('write_skew_guarded', {
    'business_rule': '在岗者>1才允许离岗',
    'observations': {
        'A': [r['row_id'] for r in a_scan],
        'B': [r['row_id'] for r in b_scan]},
    'commit_states': states,
    'final_on_call_count': sum(r['on_call'] for r in store.head().values()),
    'B_read_basis': b.read_basis(),
    'B_conflict_evidence': b.conflict_evidence,
    'serial_order_used': '按 commit 先后串行：A 先；B 的旧扫描在 A 提交后不可重放',
})
assert states == ['COMMITTED', 'CONFLICT']
assert sum(r['on_call'] for r in store.head().values()) == 1
assert b.commit() == 'CONFLICT'  # 重复提交返回第一次结果

# 3) 幻影：空范围被他人插入
ph_store = Store([dict(row_id='A', team=1, on_call=True)])
pa, pb = Transaction(ph_store, label='PA'), Transaction(ph_store, label='PB')
empty_observation = pa.scan(2, 3)
pb.put(dict(row_id='C', team=2, on_call=True))
pb.commit()
pa_state = pa.commit()
show('phantom_empty_range', {
    'empty_scan_observation': empty_observation,
    'PA_state': pa_state,
    'PA_conflict_evidence': pa.conflict_evidence,
})
assert pa_state == 'CONFLICT'

# 4) 无关 team 并行成功（不能仅因全局版本变化拒绝）
ok_store = Store([dict(row_id='A', team=1, on_call=True)])
oa, ob = Transaction(ok_store, label='OA'), Transaction(ok_store, label='OB')
oa.scan(1, 1)
ob.put(dict(row_id='C', team=5, on_call=True))
show('unrelated_team_parallel', {
    'OB_state': ob.commit(),
    'OA_state': oa.commit(),
    'final': ok_store.head(),
})
assert (ob.state, oa.state) == ('COMMITTED', 'COMMITTED')

# 5) 保存点：撤回试写不撤回读取观察，write-skew 仍被拦截
sp_store = Store([dict(row_id='a', team=1, on_call=True),
                  dict(row_id='b', team=1, on_call=True)])
t1, t2 = Transaction(sp_store, label='T1'), Transaction(sp_store, label='T2')
t1.savepoint('s')
t1_observation = t1.scan(1, 1)
t1.put(dict(row_id='b', team=1, on_call=False))   # 试写
t1.rollback_to('s')                                # 撤回试写，读取观察保留
t2.scan(1, 1)
t2.put(dict(row_id='a', team=1, on_call=False))
t2_state = t2.commit()
t1.put(dict(row_id='b', team=1, on_call=False))   # 仍依据早先观察决定
t1_state = t1.commit()
show('savepoint_read_survives_rollback', {
    'T1_observation': [r['row_id'] for r in t1_observation],
    'T2_state': t2_state,
    'T1_state': t1_state,
    'T1_read_basis': t1.read_basis(),
    'T1_conflict_evidence': t1.conflict_evidence,
    'final_table': sp_store.head(),
})
assert (t2_state, t1_state) == ('COMMITTED', 'CONFLICT')

print('demo: 全部断言通过；失败事务均未发布写入，成功事务均可按 commit 顺序串行化。')
