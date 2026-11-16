def common(left,right):
    n=0
    for a,b in zip(left,right):
        if a!=b:break
        n+=1
    return n


def encode(rows,restart=3):
    records=[]
    previous=b''
    directory=[]
    for i,(key,value) in enumerate(sorted(rows)):
        prefix=0 if i%restart==0 else common(previous,key)
        records.append(dict(prefix=prefix,suffix=key[prefix:].hex(),value=value.hex()))
        if i%restart==0:directory.append(dict(key=key.hex(),record=i))
        previous=key
    return dict(restart=restart,records=records,directory=directory)


def rename_first(page,new_key):
    record=page['records'][0]
    record['prefix']=0
    record['suffix']=new_key.hex()
    page['directory'][0]['key']=new_key.hex()
