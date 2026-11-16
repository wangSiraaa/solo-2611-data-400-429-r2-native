from reader import local_rows

def compact(store, selected, output_id):
    records=[r for run in selected for r in store.read(run)]
    points=local_rows(records)
    # Applying a deletion to all selected records does not imply that older
    # records in an unselected run no longer need its protection.
    kept=sorted(points.values(),key=lambda r:r['key'])
    store.replace(selected,output_id,kept)
    return {'selected':selected,'output':output_id,'point_records':len(kept),'erase_records':0}
