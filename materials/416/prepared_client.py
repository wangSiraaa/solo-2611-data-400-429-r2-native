from planner import plan
from predicate import evaluate


def prepare(query, name, catalog):
    return dict(query=query, index_name=name, path=plan(query, catalog[name]))


def execute(handle, rows, catalog):
    source = catalog[handle['index_name']].scan() if handle['path'] == 'partial' else rows.values()
    return sorted([row.copy() for row in source if evaluate(handle['query'], row) is True],
                  key=lambda row: row['row_id'].encode())
