import json
from pathlib import Path
from inverted import Index
from ranker import query
case=json.loads(Path('benchmark.json').read_text())
index=Index(case['docs'],case['block_size'])
rows,audit=query(index,case['terms'],case['k'])
assert rows[0][0]==case['expected_id'] and audit['scored']<case['max_scored_exclusive']
print(json.dumps(dict(result=rows,audit=audit,
    baseline_static_pruning_observed=True,
    scope='Scored count only; raw_access_probe.py separately shows query statistics reads make the old baseline fail the complete raw-access budget.')))
