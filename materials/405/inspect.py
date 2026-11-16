"""inspect：展示活动 run、保留的点与删除信息、每次回收/保留依据。"""
from reader import visible_map, sweep_work


def inspect(store):
    runs = []
    total_points = total_erases = 0
    for run_id in store.ids():
        recs = store.read(run_id)
        pts = [{'key': r['key'], 'value': r['value'], 'seq': r['seq']}
               for r in recs if r['kind'] == 'put']
        ers = [{'lo': r['lo'], 'hi': r['hi'], 'seq': r['seq']}
               for r in recs if r['kind'] == 'erase']
        total_points += len(pts)
        total_erases += len(ers)
        runs.append({'run': run_id, 'records': len(recs),
                     'points': pts, 'erases': ers})
    visible, work = sweep_work(store)
    return {
        'active_runs': store.ids(),
        'max_imported_seq': store.max_seq(),
        'physical_record_count': total_points + total_erases,
        'stored_points': total_points,
        'stored_erases': total_erases,
        'visible_key_count': len(visible),
        'visible_keys': [{'key': k, 'value': visible[k]['value'],
                          'seq': visible[k]['seq']}
                         for k in sorted(visible)],
        'read_work_measure': work,
        'compaction_journal': store.journal(),
    }
