from itertools import product
def values(document,path):
    pieces=path.split('.')
    if len(pieces)==1: return [document[pieces[0]]] if pieces[0] in document else []
    return [item[pieces[1]] for item in document.get(pieces[0],[]) if pieces[1] in item]
def keys(document,fields):
    return list(product(*(values(document,field) for field in fields)))
def build(documents,fields):
    index={}
    for document in documents:
        for key in keys(document,fields): index.setdefault(key,set()).add(document['_id'])
    return index
