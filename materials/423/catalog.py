import json
from pathlib import Path
from indexer import build
from planner import plan,execute
class Catalog:
    def __init__(self,documents,fields): self.documents={d['_id']:d for d in documents};self.fields=fields;self.index=build(documents,fields)
    def query(self,query): return execute(self.index,plan(query,self.fields))
    def put(self,document): self.documents[document['_id']]=document
    def remove(self,doc_id): self.documents.pop(doc_id,None)
if __name__=='__main__':
    import sys
    fixture=json.loads(Path(sys.argv[1]).read_text());catalog=Catalog(fixture['documents'],fixture['index'])
    print(json.dumps([catalog.query(query) for query in fixture['queries']]))
