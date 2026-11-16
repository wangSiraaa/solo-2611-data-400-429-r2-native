def match_batch(left,right):
    out=[]
    for row in left:
        candidates=[r for r in right if r['group']==row['group'] and r['ts']<=row['ts']]
        chosen=max(candidates,key=lambda r:r['ts']) if candidates else None
        out.append({'left_id':row['row_id'],'right_id':chosen['row_id'] if chosen else None})
    return out
def execute(left_source,right_source):
    output=[]
    for left,right in zip(left_source.batches(),right_source.batches()):
        output.extend(match_batch(left,right))
    return output
