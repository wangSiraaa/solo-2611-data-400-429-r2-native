def direct(r, s, t):
    return sorted([dict(r_id=x['row_id'],s_id=y['row_id'],t_id=z['row_id'],
                        a=x['a'],b=x['b'],c=y['c'],value=z['value'])
                   for x in r for y in s for z in t
                   if x['b'] is not None and y['c'] is not None
                   and x['b']==y['b'] and y['c']==z['c']],
                  key=lambda x:(x['r_id'].encode(),x['s_id'].encode(),x['t_id'].encode()))
