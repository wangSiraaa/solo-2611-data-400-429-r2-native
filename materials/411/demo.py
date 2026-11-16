import json
from storage import Store
from transaction import Transaction

seed = [dict(row_id='A', team=1, on_call=True), dict(row_id='B', team=1, on_call=True)]
normal_store = Store(seed)
normal_tx = Transaction(normal_store)
normal_tx.put(dict(row_id='C', team=2, on_call=True))
normal_ok = normal_tx.commit() == 'COMMITTED' and len(normal_store.snapshot(1)) == 3
store = Store(seed)
a, b = Transaction(store), Transaction(store)
reads = [a.scan(1,1,True), b.scan(1,1,True)]
for tx, rid in [(a,'A'),(b,'B')]:
    if len(tx.scan(1,1,True)) > 1:
        tx.put(dict(row_id=rid, team=1, on_call=False))
states = [a.commit(), b.commit()]
remaining = sum(r['on_call'] for r in store.snapshot(store.revision).values())
print(json.dumps(dict(normal_path_ok=normal_ok,
    normal_observation='独立新行事务成功提交，源含三行。',
    counterexample_detected=states == ['COMMITTED','COMMITTED'] and remaining == 0,
    actual_states=states, actual_on_call=remaining,
    failure_observation='两事务均读到2人在岗，更新不同row_id均提交，最后0人在岗。',
    oracle_basis='任一完整串行执行中，第二事务看见只剩1人便不离岗，因此最终应有1人在岗；两次旧读取无法同时成功串行化。'), ensure_ascii=False))
