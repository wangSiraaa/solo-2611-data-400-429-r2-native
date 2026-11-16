import json
from pathlib import Path
from relations import direct
def load(name):return [json.loads(line) for line in Path('fixtures/dense-'+name+'.jsonl').read_text().splitlines()]
r,s,t=load('r'),load('s'),load('t')
rows=direct(r,s,t)
print(json.dumps(dict(source_rows=len(r)+len(s)+len(t),oracle_join_rows=len(rows),
                     oracle_sum=sum(x['value'] for x in rows),
                     adjacency_source_references=len(r)+len(s)+len(t),
                     note='Only independent dense oracle and source adjacency size; not a factor-store solution.')))
