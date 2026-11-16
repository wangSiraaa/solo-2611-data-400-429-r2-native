def collapse(variants):
    fields={}
    for variant in variants:
        for name,spec in variant.items():
            t=spec['type'];fields.setdefault(name,set()).add(t)
    return fields
def accepts(row,variant):
    if set(row)!=set(variant): return False
    for key,spec in variant.items():
        value=row[key]
        if spec['type']=='literal':
            if type(value) is not type(spec['value']) or value!=spec['value']: return False
        elif spec['type']=='int':
            if type(value) is not int: return False
        elif spec['type']=='text':
            if not isinstance(value,str): return False
        elif spec['type']=='null':
            if value is not None: return False
        else: raise ValueError('unknown schema type')
    return True
