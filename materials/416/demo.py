import json
from partial_index import PartialIndex
from planner import run
from predicate import evaluate

rows={r['row_id']:r for r in [dict(row_id='positive',x=1,y=None,flag=False),
      dict(row_id='negative',x=-1,y=0,flag=False),dict(row_id='nullable',x=None,y=0,flag=True)]}
index=PartialIndex(dict(op='is_not_null',column='x'))
for row in rows.values():index.put(row)
normal_query=dict(op='eq',column='x',value=1)
normal=run(normal_query,rows,index)[0]==[rows['positive']]
query=dict(op='or',args=[dict(op='not',arg=dict(op='ge',column='x',value=0)),
                        dict(op='eq',column='flag',value=True)])
actual,path=run(query,rows,index)
expected=sorted([r for r in rows.values() if evaluate(query,r) is True],key=lambda r:r['row_id'])
print(json.dumps(dict(normal_path_ok=normal,
    normal_observation='x=1的查询安全使用非NULL部分索引并返回positive。',
    counterexample_detected=actual!=expected,path=path,
    actual=[r['row_id'] for r in actual],expected=[r['row_id'] for r in expected],
    failure_observation='NOT(x>=0) OR flag=TRUE对NULL行整体为TRUE，列名启发式仍选非NULL索引而漏nullable。',
    oracle_basis='对三条真实源行执行完整SQL三值AST，UNKNOWN OR TRUE为TRUE；可再用SQLite交叉验证。'),ensure_ascii=False))
