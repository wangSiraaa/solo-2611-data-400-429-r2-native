from planner import candidates
def query(left,right,k,max_cost=None):
    plan=candidates(left,right,k);pairs=[]
    for l in plan['left_rows']:
        for r in plan['right_rows']:
            if l['key'] is not None and l['key']==r['key'] and (max_cost is None or l['cost']+r['cost']<=max_cost):
                pairs.append({'left_id':l['id'],'right_id':r['id'],'score':l['score']+r['score']})
    return sorted(pairs,key=lambda p:(-p['score'],p['left_id'],p['right_id']))[:k]
