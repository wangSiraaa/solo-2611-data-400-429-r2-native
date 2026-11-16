import argparse,json
from pathlib import Path
from contracts import collapse,accepts
from expressions import infer,evaluate
def compile_plan(plan):
    contracts={}
    for node in plan['nodes']:
        if node['op']=='source': contracts[node['id']]=collapse(node['variants'])
        elif node['op']=='project':
            source=contracts[node['input']]
            contracts[node['id']]={name:infer(expr,source) for name,expr in node['columns'].items()}
        else: raise ValueError('unknown node')
    return contracts
def run_plan(plan,sources):
    values={}
    for node in plan['nodes']:
        if node['op']=='source':
            rows=sources[node['id']]
            if not all(any(accepts(row,v) for v in node['variants']) for row in rows): raise ValueError('schema mismatch')
            values[node['id']]=rows
        elif node['op']=='project':
            values[node['id']]=[{name:evaluate(expr,row) for name,expr in node['columns'].items()} for row in values[node['input']]]
    return values[plan['output']]
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('plan');p.add_argument('--rows');a=p.parse_args();plan=json.loads(Path(a.plan).read_text())
    contracts=compile_plan(plan)
    print(json.dumps(contracts,default=sorted))
    if a.rows: print(json.dumps(run_plan(plan,json.loads(Path(a.rows).read_text()))))
