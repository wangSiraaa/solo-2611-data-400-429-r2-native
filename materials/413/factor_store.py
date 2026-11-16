import json


def build(r, s, t, path):
    by_a = {}
    for x in r:
        left = [dict(r_id=x['row_id'],s_id=y['row_id'],b=x['b'])
                for y in s if x['b'] is not None and x['b']==y['b']]
        right = [dict(t_id=z['row_id'],c=z['c'],value=z['value']) for z in t
                 if any(y['c'] is not None and y['c']==z['c'] for y in s
                        if x['b'] is not None and x['b']==y['b'])]
        group = by_a.setdefault(x['a'],dict(left=[],right=[]))
        group['left'].extend(left)
        group['right'].extend(right)
    with open(path,'w') as handle:
        json.dump(by_a,handle)


def open_store(path):
    with open(path) as handle:
        return json.load(handle)
