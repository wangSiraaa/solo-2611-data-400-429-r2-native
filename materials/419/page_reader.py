def decode_record(record,previous):
    prefix=record['prefix']
    if prefix>len(previous):raise ValueError('PREFIX_OUT_OF_RANGE')
    return previous[:prefix]+bytes.fromhex(record['suffix']),bytes.fromhex(record['value'])


def decode_all(page):
    previous=b''
    result=[]
    for record in page['records']:
        key,value=decode_record(record,previous)
        result.append((key,value))
        previous=key
    return result


def seek(page,key):
    restart=None
    for entry in page['directory']:
        if bytes.fromhex(entry['key'])<=key:restart=entry
    if restart is None:return None
    previous=b''
    start=restart['record']
    for record in page['records'][start:start+page['restart']]:
        current,value=decode_record(record,previous)
        if current==key:return value
        if current>key:return None
        previous=current
    return None
