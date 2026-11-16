from predicate import columns,evaluate


def plan(query,index):
    return 'partial' if columns(index.predicate)<=columns(query) else 'scan'


def run(query,rows,index):
    path=plan(query,index)
    source=index.scan() if path=='partial' else rows.values()
    return sorted([r.copy() for r in source if evaluate(query,r) is True],
                  key=lambda r:r['row_id'].encode()),path
