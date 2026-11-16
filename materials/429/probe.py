import json
from pathlib import Path
from sources import Source
from ranked_join import query
root=Path(__file__).resolve().parent;fixture=json.loads((root/'fixture.json').read_text())
left=Source(fixture['left']);right=Source(fixture['right']);actual=query(left,right,2)
oracle=[]
for l in fixture['left']:
    for r in fixture['right']:
        if l['key'] is not None and l['key']==r['key']:oracle.append({'left_id':l['id'],'right_id':r['id'],'score':l['score']+r['score']})
oracle=sorted(oracle,key=lambda p:(-p['score'],p['left_id'],p['right_id']))[:2]
normal_left=Source([{'id':'a','key':'x','score':10,'cost':1}]);normal_right=Source([{'id':'b','key':'x','score':9,'cost':1}])
benchmark_left=[{'id':'L%03d'%i,'key':'x' if i<20 else 'left-only','score':1000-i if i<20 else -i,'cost':0} for i in range(200)]
benchmark_right=[{'id':'R%03d'%i,'key':'x' if i<20 else 'right-only','score':1000-i if i<20 else -i,'cost':0} for i in range(200)]
bl,br=Source(benchmark_left),Source(benchmark_right);prefix_left,prefix_right=bl.top(4),br.top(4)
prefix_pairs=sorted([{'left_id':l['id'],'right_id':r['id'],'score':l['score']+r['score']} for l in prefix_left for r in prefix_right if l['key']==r['key']],key=lambda p:(-p['score'],p['left_id'],p['right_id']))[:5]
unseen_bound=max(prefix_left[-1]['score']+prefix_right[0]['score'],prefix_right[-1]['score']+prefix_left[0]['score'])
benchmark={'source_rows':400,'actual_prefix_rows_returned':sum(c['returned'] for c in bl.calls+br.calls),'prefix_top5':prefix_pairs,'unseen_pair_score_upper_bound':unseen_bound,'kth_score':prefix_pairs[-1]['score'],'strict_bound_proves_prefix_sufficient':unseen_bound<prefix_pairs[-1]['score']}
print(json.dumps({'benchmark_feasibility':benchmark,'normal_single_pair':query(normal_left,normal_right,1),'local_topk_actual':actual,'independent_full_join_topk':oracle,
                  'actual_calls':{'left':left.calls,'right':right.calls},
                  'feasibility':'Complete small join contains four legal pair identities; matching right-key lookups and a score frontier can retrieve second-best l2/r1 without reading unrelated tails.'}))
