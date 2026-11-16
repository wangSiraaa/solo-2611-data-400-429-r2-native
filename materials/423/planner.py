def plan(query,fields):
    conditions=query.get('elemMatch',query.get('and',[]))
    return {'bounds':{fields.index(term['field']):term['value'] for term in conditions if term['op']=='eq'},'residual':False}
def execute(index,plan):
    result=set()
    for key,ids in index.items():
        if all(key[column]==value for column,value in plan['bounds'].items()): result.update(ids)
    return sorted(result)
