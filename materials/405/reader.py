def local_rows(records):
    points={}
    tombs=[r for r in records if r['kind']=='erase']
    for r in records:
        if r['kind']!='put':continue
        if any(t['lo']<=r['key']<t['hi'] and t['seq']>r['seq'] for t in tombs):continue
        if r['key'] not in points or r['seq']>points[r['key']]['seq']:points[r['key']]=r
    return points

def scan(store, lo, hi):
    merged={}
    for run in store.ids():
        # Per-run reduction works when all records sharing a key are in one run.
        for key, row in local_rows(store.read(run)).items():
            if lo<=key<hi and (key not in merged or row['seq']>merged[key]['seq']):merged[key]=row
    return [[key,merged[key]['value']] for key in sorted(merged)]

def get(store,key):
    rows=scan(store,key,key+1)
    return rows[0][1] if rows else None
