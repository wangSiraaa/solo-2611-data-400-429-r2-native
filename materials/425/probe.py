import json
from pathlib import Path
from reader import ChunkSource
from asof import execute,match_batch
root=Path(__file__).resolve().parent
left=ChunkSource(root/'left.json');right=ChunkSource(root/'right.json')
actual=execute(left,right)
left_all=json.loads((root/'chunks/l0.json').read_text())+json.loads((root/'chunks/l1.json').read_text())
right_all=json.loads((root/'chunks/r0.json').read_text())+json.loads((root/'chunks/r1.json').read_text())
oracle=[]
for l in left_all:
    eligible=[r for r in right_all if r['group']==l['group'] and r['ts']<=l['ts']]
    winner=sorted(eligible,key=lambda r:(-r['ts'],r['row_id']))[0] if eligible else None
    oracle.append({'left_id':l['row_id'],'right_id':winner['row_id'] if winner else None})
print(json.dumps({'normal_single_batch':match_batch(left_all,right_all),'chunked_actual':actual,'independent_oracle':oracle,
                  'source_rows_read':left.rows_read+right.rows_read,'fixed_example_resident_rows':len(left_all)+len(right_all),
                  'feasibility':'The four actual rows fit a 32-row workspace. A stream cursor or a bounded local temporal index can retain predecessor candidates across source chunk boundaries.'}))
