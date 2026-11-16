import json
from pathlib import Path
from storage import Store
from transaction import Transaction

case = json.loads(Path(__file__).with_name('savepoint-cases.json').read_text())
store = Store(case['initial'])
left, right = Transaction(store), Transaction(store)
saved_writes = dict(left.writes)  # Legacy caller's local write rollback, not a target implementation.
left_observation = left.scan(1, 1)
left.put(dict(row_id='b', team=1, on_call=False))
left.writes = saved_writes
right_observation = right.scan(1, 1)
right.put(dict(row_id='a', team=1, on_call=False))
right_result = right.commit()
left.put(dict(row_id='b', team=1, on_call=False))
left_result = left.commit()
assert all(row['on_call'] for row in left_observation + right_observation)
assert (left_result, right_result) == ('COMMITTED', 'COMMITTED')
# If left precedes right, right must observe b=false; if right precedes left,
# left must observe a=false. Neither serial order reproduces both observations.
assert all(not row['on_call'] for row in store.snapshot(store.revision).values())
print(json.dumps({'baseline_both_committed': True, 'valid_serial_orders': 0,
                  'target_savepoint_api_present': hasattr(left, 'savepoint'),
                  'scope': 'Observed baseline anomaly with legacy caller write rollback; no target savepoint implemented.'}))
