def infer(expr,fields):
    if 'field' in expr:
        name=expr['field']
        if name not in fields: raise ValueError('unknown field '+name)
        return fields[name]
    if 'const' in expr:
        value=expr['const'];return {'int' if type(value) is int else 'text' if isinstance(value,str) else 'null'}
    op=expr['op'];args=expr['args']
    if op=='eq':
        for arg in args: infer(arg,fields)
        return {'bool'}
    if op=='if':
        infer(args[0],fields);return infer(args[1],fields)|infer(args[2],fields)
    if op=='add':
        types=[infer(arg,fields) for arg in args]
        if any(t!={'int'} for t in types): raise ValueError('add requires integer')
        return {'int'}
    raise ValueError('unknown op '+op)
def evaluate(expr,row):
    if 'field' in expr: return row[expr['field']]
    if 'const' in expr: return expr['const']
    op=expr['op'];args=expr['args']
    if op=='eq':
        a=evaluate(args[0],row);b=evaluate(args[1],row);return type(a) is type(b) and a==b
    if op=='if': return evaluate(args[1] if evaluate(args[0],row) else args[2],row)
    if op=='add': return sum(evaluate(arg,row) for arg in args)
    raise ValueError(op)
