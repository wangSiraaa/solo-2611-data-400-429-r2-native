def matches(row,predicate):
    if predicate['op']=='all': return True
    if predicate['op']=='eq': return row.get(predicate['field'])==predicate['value']
    if predicate['op']=='and': return all(matches(row,p) for p in predicate['args'])
    raise ValueError('unknown predicate')
def compile_checks(checks):
    populations=[check['population'] for check in checks]
    return {'checks':checks,'accept':lambda row:any(matches(row,population) for population in populations)}
