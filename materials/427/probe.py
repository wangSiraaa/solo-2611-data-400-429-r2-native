import json
from pathlib import Path
from source import Source
from compiler import matches
from engine import run,evaluate
root=Path(__file__).resolve().parent
checks=json.loads((root/'checks.json').read_text());source=Source(root/'rows.jsonl');actual=run(source,checks)
rows=[json.loads(line) for line in (root/'rows.jsonl').read_text().splitlines()]
oracle=[evaluate(check,[row for row in rows if matches(row,check['population'])]) for check in checks]
normal_checks=[{**check,'population':{'op':'eq','field':'team','value':'A'}} for check in checks]
print(json.dumps({'normal_same_population':run(Source(root/'rows.jsonl'),normal_checks),'fused_actual':actual,'independent_per_check_oracle':oracle,
                  'source_decodes':source.decoded,'source_scans':source.scans,
                  'feasibility':'The same one-pass decoded rows can feed two independent population predicates; tiny oracle shows exact per-check counts, not only overall pass/fail.'}))
