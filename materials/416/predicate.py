def evaluate(expr,row):
    op=expr['op']
    if op in ('and','or'):
        values=[evaluate(e,row) for e in expr['args']]
        if op=='and':
            return False if False in values else (None if None in values else True)
        return True if True in values else (None if None in values else False)
    if op=='not':
        value=evaluate(expr['arg'],row)
        return None if value is None else not value
    value=row[expr['column']]
    if op=='is_null':return value is None
    if op=='is_not_null':return value is not None
    target=expr['value']
    if value is None or target is None:return None
    return {'eq':lambda:value==target,'ne':lambda:value!=target,
            'lt':lambda:value<target,'le':lambda:value<=target,
            'gt':lambda:value>target,'ge':lambda:value>=target}[op]()


def columns(expr):
    if expr['op'] in ('and','or'):
        return set().union(*(columns(e) for e in expr['args']))
    if expr['op']=='not':return columns(expr['arg'])
    return {expr['column']}
