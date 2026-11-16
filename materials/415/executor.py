"""执行器：execute 时按当前源状态重新裁决 join 消除。

PreparedPlan 里没有永久的“无条件可删 join”结论；每次 execute 都：
  1. 固定本次执行的源状态 state_version；
  2. 用目录可信资格（同版本）尝试消除；证据不足时可改走运行时守卫；
  3. 守卫通过才消除，守卫失败则 fallback 朴素 join；
  4. 真实访问计数区分 query_rows（算子业务行）与 guard_rows（守卫读取），
     被消除的父表不会出现在 query 访问轨迹里。

NULL 与重复数是两条路径都显式处理的前提；无证据时保留 join 是允许的安全路径。
"""

from planner import prepare

_OPS = {
    'eq': lambda a, b: a is not None and a == b,
    'ne': lambda a, b: a is not None and a != b,
    'lt': lambda a, b: a is not None and a < b,
    'le': lambda a, b: a is not None and a <= b,
    'gt': lambda a, b: a is not None and a > b,
    'ge': lambda a, b: a is not None and a >= b,
}


class QueryResult:
    def __init__(self, rows, physical_plan, access_trace, state_version, decision_trace):
        self.rows = rows
        self.physical_plan = physical_plan
        self.access_trace = access_trace
        self.state_version = state_version
        self.decision_trace = decision_trace

    def as_dict(self):
        return dict(rows=self.rows, physical_plan=self.physical_plan,
                    access_trace=self.access_trace, state_version=self.state_version,
                    decision_trace=self.decision_trace)


def execute(prepared, db, catalog=None, strategy='auto', counters=None):
    """执行 PreparedPlan。

    strategy:
      'naive'  永不消除，朴素嵌套循环 join（也作为 oracle 对照入口）；
      'catalog' 只接受同版本可信目录证据，不足则保留 join；
      'guard'   不用目录，运行时守卫当前数据后决定消除或 fallback；
      'auto'   优先目录证据，缺失时对形状合格的 join 尝试运行时守卫。
    """
    if isinstance(prepared, dict) and 'plan' in prepared:
        logical = prepared['plan']
    else:
        logical = prepared
    version = db.state_version
    trace = {}
    decisions = []
    node = _build(logical, db, catalog, strategy, version, trace, decisions)
    drows = node['eval']()
    ordered = sorted(drows, key=lambda d: d['prov'])
    rows = [_project(d['bag'], logical['projection']) for d in ordered]
    plan_desc = node['describe']()
    if counters is not None:
        counters.update(trace)
    return QueryResult(rows, plan_desc, dict(trace), version, decisions)


# ---------------------------------------------------------------- 物理计划构建

def _build(node, db, catalog, strategy, version, trace, decisions):
    if node['op'] == 'scan':
        return _scan_node(node, db, trace)
    left = _build(node['left'], db, catalog, strategy, version, trace, decisions)
    right_scan = node['right']  # 形状已在 prepare 校验：候选消除时为裸 scan
    left_key_table, left_key_col = _split_key(node['left_key'])
    if left_key_table is None:
        left_key_table = _leftmost_table(node['left'])
    node_for_decision = dict(node, left_table=left_key_table,
                             left_key_resolved=left_key_col)
    decision = _decide(node_for_decision, catalog, strategy, version)
    if decision['eliminable'] and decision['path'] is not None:
        if decision['path'] == 'catalog':
            phys = _eliminated_node(node_for_decision, left, right_scan, decision, trace)
        else:
            phys = _guard_node(node_for_decision, left, right_scan, db,
                               decision, trace, decisions)
        decisions.append(decision)
        return phys
    # 安全路径：保留真实 join
    decision.setdefault('eliminable', False)
    decisions.append(decision)
    return _naive_join_node(node_for_decision, left, right_scan, db, trace, decision)


def _decide(join, catalog, strategy, version):
    left_table = join.get('left_table') or join['left']['table']
    left_key = join.get('left_key_resolved', join['left_key'])
    base = dict(join=dict(kind=join['kind'],
                          left=left_table, left_key=join['left_key'],
                          right=join['right']['table'], right_key=join['right_key']),
                eliminable=False, path=None, prerequisites=[], missing=[])
    if not join.get('_eliminable_shape', True):
        base['reason'] = 'right side carries filters or nested ops; join retained'
        return base
    base['prerequisites'] = _prerequisites(join, left_table, left_key)
    if strategy == 'naive':
        base['reason'] = 'naive strategy requested'
        return base
    if catalog is not None and strategy in ('catalog', 'auto'):
        evidence = catalog.join_evidence(
            join['kind'], left_table, left_key,
            join['right']['table'], join['right_key'], version)
        if evidence['complete']:
            base.update(eliminable=True, path='catalog', reason='trusted evidence',
                        evidence=evidence)
            return base
        base['missing'] = evidence['missing']
    if strategy in ('guard', 'auto'):
        base['candidate_path'] = 'runtime_guard'
        base.setdefault('reason', 'catalog evidence incomplete; guard available')
        # 守卫是否通过要读数据后才知道，先标记候选，由 _guard_node 运行时裁决
        base['eliminable'] = True
        base['path'] = 'guard'
        if catalog is None:
            base['reason'] = 'no catalog; runtime guard will verify live data'
        return base
    base['reason'] = 'catalog evidence incomplete and guard disabled'
    return base


def _prerequisites(join, left_table, left_key):
    common = [
        'at_most_one_match: UNIQUE(%s.%s) — 父键重复会放大行数'
        % (join['right']['table'], join['right_key'])]
    if join['kind'] == 'left':
        return ['LEFT 无匹配本就保留左行，仅需'] + common
    return [
        'referential_existence: FOREIGN_KEY(%s.%s -> %s.%s) — 孤儿行会被 INNER 删除'
        % (left_table, left_key,
           join['right']['table'], join['right_key']),
        'null_handling: NOT_NULL(%s.%s) — 等值连接中 NULL 不匹配，NULL 子行会被删除'
        % (left_table, left_key),
    ] + common


def _leftmost_table(node):
    while node['op'] != 'scan':
        node = node['left']
    return node['table']


# ---------------------------------------------------------------- 物理算子

def _scan_node(scan, db, trace):
    def eval_scan():
        stored = db._scan(scan['table'])
        trace[scan['table']] = trace.get(scan['table'], 0) + len(stored)
        db.ledger.add('query_rows', scan['table'], len(stored))
        out = []
        for rec in stored:
            if all(_filter_match(rec['data'], f) for f in scan['filters']):
                out.append(dict(prov=(rec['seq'],),
                                bag={scan['table']: dict(rec['data'])}))
        return out

    def describe():
        return dict(op='scan', table=scan['table'], filters=list(scan['filters']),
                    rows_read=trace.get(scan['table'], 0))
    return dict(eval=eval_scan, describe=describe)


def _naive_join_node(join, left, right_scan, db, trace, decision):
    left_table = join['left_table'] if 'left_table' in join else join['left']['table']

    def eval_join():
        left_rows = left['eval']()
        stored = db._scan(right_scan['table'])
        trace[right_scan['table']] = trace.get(right_scan['table'], 0) + len(stored)
        db.ledger.add('query_rows', right_scan['table'], len(stored))
        right_rows = [r for r in stored
                      if all(_filter_match(r['data'], f) for f in right_scan['filters'])]
        out = []
        for d in left_rows:
            lval = _col(d, left_table, join['left_key_resolved'])
            matches = [r for r in right_rows
                       if lval is not None
                       and r['data'].get(join['right_key']) is not None
                       and lval == r['data'].get(join['right_key'])]
            if not matches and join['kind'] == 'left':
                out.append(dict(prov=d['prov'] + (-1,),
                                bag=dict(d['bag'])))
                continue
            for r in matches:  # 父键重复时每个匹配产生一行，保留放大
                merged = dict(d['bag'])
                merged.setdefault(right_scan['table'], {})
                merged[right_scan['table']] = dict(r['data'])
                out.append(dict(prov=d['prov'] + (r['seq'],), bag=merged))
        return out

    def describe():
        return dict(op='join', kind=join['kind'], retained=True,
                    reason=decision.get('reason', 'insufficient evidence'),
                    missing=decision.get('missing', []),
                    left=left['describe'](),
                    right=dict(op='scan', table=right_scan['table'],
                               rows_read=trace.get(right_scan['table'], 0)))
    return dict(eval=eval_join, describe=describe)


def _eliminated_node(join, left, right_scan, decision, trace):
    def eval_eliminated():
        # 目录已在同一源状态担保存在性/唯一性/NULL 前提：直接返回左子树，
        # 执行期不读取被消除父表的任何业务行。
        return left['eval']()

    def describe():
        return dict(op='eliminated_join', kind=join['kind'],
                    original_right_table=right_scan['table'],
                    right_table_query_reads=0,
                    path='catalog', prerequisites=decision['prerequisites'],
                    evidence=decision.get('evidence'),
                    child=left['describe']())
    return dict(eval=eval_eliminated, describe=describe)


def _guard_node(join, left, right_scan, db, decision, trace, decisions):
    left_table = join['left_table'] if 'left_table' in join else join['left']['table']

    def eval_guard():
        left_rows = left['eval']()
        stored = db._scan(right_scan['table'])
        # 守卫读取单独记账，不算查询算子的业务行访问。
        db.ledger.add('guard_rows', right_scan['table'], len(stored))
        right_counts = {}
        for rec in stored:
            val = rec['data'].get(join['right_key'])
            if val is None:
                continue
            key = _json_key(val)
            right_counts[key] = right_counts.get(key, 0) + 1
        distinct_left = {}
        null_left = 0
        for d in left_rows:
            lval = _col(d, left_table, join['left_key_resolved'])
            if lval is None:
                null_left += 1
            else:
                distinct_left[_json_key(lval)] = distinct_left.get(_json_key(lval), 0) + 1
        failures = []
        for key, n in right_counts.items():
            if n > 1:
                failures.append('duplicate parent key count=%d' % n)
        if join['kind'] == 'inner':
            if null_left:
                failures.append('null child key rows=%d (INNER would drop them)' % null_left)
            for key in distinct_left:
                if right_counts.get(key, 0) == 0:
                    failures.append('child key without parent match (INNER would drop it)')
        # LEFT：NULL/无匹配本就保留，只需至多一个匹配
        if failures:
            decision.update(eliminable=False, path=None,
                            reason='runtime guard failed; fell back to naive join',
                            guard_failures=failures)
            return _naive_join_node(join, left, right_scan, db, trace, decision)['eval']()
        decision.update(eliminable=True, path='guard',
                        reason='runtime guard verified live data',
                        guard_stats=dict(distinct_left_keys=len(distinct_left),
                                         null_left_keys=null_left,
                                         parent_keys_checked=len(right_counts)))
        return left_rows

    def describe():
        if decision.get('guard_failures'):
            return dict(op='join', kind=join['kind'], retained=True,
                        reason='runtime guard failed; fell back to naive join',
                        guard_failures=decision['guard_failures'],
                        left=left['describe'](),
                        right=dict(op='scan', table=right_scan['table'],
                                   rows_read=trace.get(right_scan['table'], 0)))
        return dict(op='guarded_join', kind=join['kind'],
                    original_right_table=right_scan['table'],
                    right_table_query_reads=0,
                    path=decision.get('path'),
                    prerequisites=decision['prerequisites'],
                    guard_stats=decision.get('guard_stats'),
                    child=left['describe']())
    return dict(eval=eval_guard, describe=describe)


# ---------------------------------------------------------------- 工具函数

def _filter_match(row, flt):
    return _OPS[flt['op']](row.get(flt['column']), flt['value'])


def _col(drow, table, column):
    bag = drow['bag']
    # 限定名 parent.gid：必须从对应来源表的 bag 取
    if table in bag and column in bag[table]:
        return bag[table][column]
    if table in bag:
        return None
    for t, row in bag.items():
        if column in row:
            return row[column]
    return None


def _split_key(key):
    """等值连接键可能是限定名 'parent.gid'，返回 (来源表或 None, 列名)。"""
    if '.' in key:
        return tuple(key.split('.', 1))
    return None, key


def _project(bag, projection):
    out = {}
    for col in projection:
        if '.' in col:
            table, column = col.split('.', 1)
            out[col] = bag.get(table, {}).get(column)
        else:
            out[col] = _flat_get(bag, col)
    return out


def _flat_get(bag, column):
    for row in bag.values():
        if column in row:
            return row[column]
    return None


def _json_key(value):
    if isinstance(value, (list, dict)):
        import json
        return ('json', json.dumps(value, sort_keys=True, ensure_ascii=False))
    if isinstance(value, bool):
        return ('bool', value)
    return (type(value).__name__, value)


def run(query, db, catalog=None, strategy='auto', counters=None):
    """便捷入口：prepare + execute。"""
    return execute(prepare(query), db, catalog, strategy, counters)
