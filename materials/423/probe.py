import json
from pathlib import Path
from catalog import Catalog
root=Path(__file__).resolve().parent
fixture=json.loads((root/'fixture.json').read_text());catalog=Catalog(fixture['documents'],fixture['index'])
actual=catalog.query(fixture['queries'][0])
oracle=sorted(d['_id'] for d in fixture['documents'] if any(i.get('sku')=='A' and i.get('qty')==5 for i in d['items']))
normal=Catalog([{'_id':'normal','tenant':'t','enabled':True,'items':[{'sku':'A','qty':5}]}],fixture['index']).query(fixture['queries'][0])
catalog.put({'_id':'mixed','tenant':'t','enabled':True,'items':[{'sku':'Z','qty':9}]})
after=catalog.query(fixture['queries'][0])
print(json.dumps({'normal_single_element':normal,'elemMatch_actual':actual,'independent_oracle':oracle,'after_put_actual':after,
                  'mixed_keys':sorted([list(k) for k,ids in catalog.index.items() if 'mixed' in ids]),
                  'feasibility':'Direct element-level oracle accepts only same. Independent unbound predicates would legitimately accept mixed as well; same data can distinguish bound scope from plain AND.'}))
