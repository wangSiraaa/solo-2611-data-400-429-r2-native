def expand(store):
    rows = []
    for a, group in store.items():
        for left in group['left']:
            for right in group['right']:
                rows.append(dict(a=a,**left,**right))
    return sorted(rows,key=lambda x:(x['r_id'].encode(),x['s_id'].encode(),x['t_id'].encode()))


def aggregate(store):
    rows = expand(store)
    values = [r['value'] for r in rows if r['value'] is not None]
    return dict(count=len(rows),sum=sum(values) if values else None)
