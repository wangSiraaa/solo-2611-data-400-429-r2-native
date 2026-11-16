import json,tempfile
from run_store import Store
from reader import scan
from compactor import compact
P=lambda key,value,seq:{'kind':'put','key':key,'value':value,'seq':seq}
D=lambda lo,hi,seq:{'kind':'erase','lo':lo,'hi':hi,'seq':seq}
with tempfile.TemporaryDirectory() as d:
    s=Store(d);s.add('one',[P(10,'old',1),D(0,20,2),P(12,'new',3)])
    print(json.dumps({'case':'one_run','actual':scan(s,-100,100),'oracle':[[12,'new']]}))
with tempfile.TemporaryDirectory() as d:
    s=Store(d);s.add('old',[P(10,'old',1),P(30,'keep',2)]);s.add('delete',[D(0,20,3)]);s.add('fresh',[P(12,'new',4)])
    print(json.dumps({'case':'cross_run','actual':scan(s,-100,100),'oracle':[[12,'new'],[30,'keep']]}))
    receipt=compact(s,['delete','fresh'],'merged')
    print(json.dumps({'case':'partial_compact','receipt':receipt,'actual':scan(s,-100,100),'oracle':[[12,'new'],[30,'keep']], 'stored_runs':{r:s.read(r) for r in s.ids()}}))
