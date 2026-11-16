import json


def save(page,path):
    payload=json.dumps(page,sort_keys=True,separators=(',',':')).encode()
    with open(path,'wb') as target:target.write(payload)


def load(path):
    with open(path,'rb') as source:return json.load(source)
