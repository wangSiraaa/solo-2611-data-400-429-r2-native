from compiler import compile_checks
def evaluate(check,rows):
    if check['kind']=='not_null':
        bad=[r['row_id'] for r in rows if r.get(check['field']) is None]
    elif check['kind']=='unique':
        groups={}
        for row in rows: groups.setdefault(tuple(row.get(c) for c in check['fields']),[]).append(row['row_id'])
        bad=[row_id for ids in groups.values() if len(ids)>1 for row_id in ids]
    else: raise ValueError('unknown check kind')
    return {'check_id':check['check_id'],'population_count':len(rows),'violation_count':len(bad),'violating_rows':sorted(bad),'passed':not bad}
def run(source,checks):
    plan=compile_checks(checks);rows=[row for row in source.scan() if plan['accept'](row)]
    return [evaluate(check,rows) for check in checks]
