def execute(plan,tables,counters=None):
    counters={} if counters is None else counters
    if plan['op']=='scan':
        rows=tables[plan['table']]
        counters[plan['table']]=counters.get(plan['table'],0)+len(rows)
        return [{k:r[k] for k in plan['projection']} for r in rows]
    left=tables[plan['left']['table']]
    right=tables[plan['right']['table']]
    counters['left_rows']=len(left)
    counters['right_rows']=len(right)
    output=[]
    for row in left:
        matches=[p for p in right if row[plan['left_key']] is not None
                 and p[plan['right_key']] is not None
                 and row[plan['left_key']]==p[plan['right_key']]]
        if not matches and plan['kind']=='left':matches=[None]
        for match in matches:output.append({k:row[k] for k in plan['projection']})
    return output
