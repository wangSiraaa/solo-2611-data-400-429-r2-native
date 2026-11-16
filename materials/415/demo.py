import json
from catalog import Catalog
from planner import optimize
from executor import execute

catalog=Catalog()
catalog.declare_fk('child','parent_id','parent','id')
plan=dict(op='join',kind='inner',left=dict(table='child'),right=dict(table='parent'),
          left_key='parent_id',right_key='id',projection=['row_id'])
tables=dict(parent=[dict(row_id='p',id=1)],child=[dict(row_id='ok',parent_id=1)])
normal=execute(optimize(plan,catalog),tables)==execute(plan,tables)
tables['child'].append(dict(row_id='orphan',parent_id=2))
actual=execute(optimize(plan,catalog),tables)
expected=execute(plan,tables)
print(json.dumps(dict(normal_path_ok=normal,
    normal_observation='所有子行恰有一个父匹配时，消除INNER保持相同结果。',
    counterexample_detected=actual!=expected,actual=actual,expected=expected,
    failure_observation='未验证的FK有孤儿行，消除INNER返回ok和orphan，而朴素join只有ok。',
    oracle_basis='对真实父子bag逐对执行SQL式非NULL等值，保留匹配出现次数。'),ensure_ascii=False))
