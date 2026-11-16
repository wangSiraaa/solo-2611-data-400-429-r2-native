import json
from evaluator import Evaluator
from relations import execute

source = dict(op='scan', rows=[dict(row_id='r', x=-7)])
immutable = dict(call='abs', args=[dict(column='x')])
normal_eval = Evaluator(10)
normal = execute(dict(op='project', input=source, columns=[['a',immutable],['b',immutable]]),normal_eval)
volatile = dict(call='next_ticket', args=[])
evaluator = Evaluator(10)
actual = execute(dict(op='project', input=source, columns=[['a',volatile],['b',volatile]]),evaluator)
expected = [dict(a=1,b=2)]
print(json.dumps(dict(normal_path_ok=normal==[dict(a=7,b=7)] and normal_eval.calls==['abs'],
    normal_observation='immutable abs双引用返回7/7且真实调用一次。',
    counterexample_detected=actual!=expected, actual=actual, expected=expected, calls=evaluator.calls,
    failure_observation='同一行两个next_ticket表达式被CSE合并，实际1/1、只调用一次，约定应1/2、调用两次。',
    oracle_basis='声明顺序逐字段直接调用无CSE的小解释，next_ticket从1递增。'),ensure_ascii=False))
