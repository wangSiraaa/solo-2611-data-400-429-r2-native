import json
from pathlib import Path
import tempfile
from relations import direct
from factor_store import build,open_store
from consumer import expand

r=[dict(row_id='r1',a='A',b=1)]
s=[dict(row_id='s1',b=1,c=10)]
t=[dict(row_id='t1',c=10,value=7)]
with tempfile.TemporaryDirectory() as folder:
    target=Path(folder)/'f.json'
    build(r,s,t,target)
    normal=expand(open_store(target))==direct(r,s,t)
    r.append(dict(row_id='r2',a='A',b=2))
    s.append(dict(row_id='s2',b=2,c=20))
    t.append(dict(row_id='t2',c=20,value=11))
    build(r,s,t,target)
    actual=expand(open_store(target))
    expected=direct(r,s,t)
    print(json.dumps(dict(normal_path_ok=normal,
        normal_observation='单条关联链可写入、重新打开并正确展开。',
        counterexample_detected=actual!=expected,actual_count=len(actual),expected_count=len(expected),
        actual_identities=[[x['r_id'],x['s_id'],x['t_id']] for x in actual],
        failure_observation='同一a下两条独立b-c链被误作笛卡尔积，生成4个出现，正确仅2个。',
        oracle_basis='小样本对R/S/T逐来源三重循环，仅在两条等值边都成立时产出身份。'),ensure_ascii=False))
