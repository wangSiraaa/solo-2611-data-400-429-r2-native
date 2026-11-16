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


engine = start()
before = engine.snapshot()
apply(engine, case['swap'])
delta = engine.snapshot() - before
engine = start()
before = engine.snapshot()
try:
    apply(engine, case['late_invalid'])
except ValueError:
    pass
partial = engine.snapshot() != before
assert delta == 2 and partial
print(json.dumps({'baseline_swap_commit_delta': delta, 'required_swap_commit_delta': 1,
                  'baseline_failed_batch_changed_state': partial,
                  'scope': 'Runs existing loader and Engine; target atomic apply is not implemented.'}))
