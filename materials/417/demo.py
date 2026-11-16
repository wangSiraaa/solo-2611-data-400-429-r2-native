import json
from inverted import Index
from ranker import query
from scorer import score,statistics

docs={'A':['alpha'],'B':['beta']*10}
docs.update({f'Z{i:02}':['beta'] for i in range(20)})
index=Index(docs)
terms=['alpha','beta']
def oracle():
    stats=statistics(index.docs)
    return sorted([(k,score(terms,t,stats)) for k,t in index.docs.items()
                   if any(x in t for x in terms)],key=lambda x:(-x[1],x[0].encode()))[:1]
normal=query(index,terms,1)[0]==oracle()
for i in range(20):index.remove(f'Z{i:02}')
actual,audit=query(index,terms,1)
expected=oracle()
print(json.dumps(dict(normal_path_ok=normal,
    normal_observation='静态22篇语料的块剪枝top1与全评分一致。',
    counterexample_detected=actual!=expected,actual=actual,expected=expected,audit=audit,
    failure_observation='删去20篇beta文档后旧bound剪掉B，返回A；全评分当前统计的真正top1是B。',
    oracle_basis='对当前活跃文档逐个调用固定BM25 kernel并完整排序，完全不使用block bound。'),ensure_ascii=False))
