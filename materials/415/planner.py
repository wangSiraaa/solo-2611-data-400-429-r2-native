"""逻辑计划准备（prepare）：只做规范化，不产生永久的“可删 join”结论。

输入是 typed JSON 查询描述（不解析 SQL），最多两层 INNER/LEFT 等值 join，
可带来源侧等值/范围过滤，projection 只引用指定源表的列。

prepare() 输出 PreparedPlan：
  * plan          规范化逻辑算子树（仍是朴素 join，不删任何算子）；
  * explain       仅供参考的当前目录建议，显式标注 non_binding；
真正是否消除由 executor 在 execute 时按当前 state_version 重新裁决。
"""

MAX_JOIN_DEPTH = 2


def prepare(query, catalog=None):
    plan = _normalize_node(query)
    _validate(plan)
    explain = None
    if catalog is not None:
        explain = _advisory(plan, catalog)
    return dict(plan=plan, explain=explain,
                note='advisory only; execute re-decides against the live source state')


def _normalize_node(node):
    op = node['op']
    if op == 'scan':
        out = dict(op='scan', table=node['table'],
                   projection=list(node.get('projection', [])),
                   filters=[_normalize_filter(f) for f in node.get('filters', [])])
        return out
    if op == 'join':
        out = dict(op='join', kind=node['kind'],
                   left=_normalize_node(node['left']),
                   right=_normalize_node(node['right']),
                   left_key=node['left_key'], right_key=node['right_key'],
                   projection=list(node.get('projection', [])))
        return out
    raise ValueError('unknown op: %r' % op)


def _normalize_filter(f):
    """来源侧过滤：{column, op: eq|ne|lt|le|gt|ge, value}。"""
    if f['op'] not in ('eq', 'ne', 'lt', 'le', 'gt', 'ge'):
        raise ValueError('unsupported filter op: %r' % f['op'])
    return dict(column=f['column'], op=f['op'], value=f['value'])


def _validate(plan):
    joins = _count_joins(plan)
    if joins > MAX_JOIN_DEPTH:
        raise ValueError('at most %d joins allowed' % MAX_JOIN_DEPTH)
    if plan['op'] == 'join':
        _check_join_shape(plan)
    _check_projection_sources(plan, _output_tables(plan))


def _count_joins(node):
    if node['op'] == 'scan':
        return 0
    return 1 + _count_joins(node['left']) + _count_joins(node['right'])


def _check_join_shape(join):
    # 被消除的候选要求右侧是不带父侧过滤的裸 scan；带父侧条件的 join 保留即可。
    right = join['right']
    if right['op'] != 'scan' or right['filters']:
        join['_eliminable_shape'] = False
    else:
        join['_eliminable_shape'] = True
    if join['left']['op'] == 'join':
        _check_join_shape(join['left'])


def _output_tables(node):
    if node['op'] == 'scan':
        return {node['table']}
    return _output_tables(node['left']) | _output_tables(node['right'])


def _check_projection_sources(node, output_tables):
    for col in node['projection']:
        table = col.split('.', 1)[0] if '.' in col else None
        if table is not None and table not in output_tables:
            raise ValueError('projection references unknown table: %r' % col)


def _advisory(plan, catalog):
    """静态建议：只反映目录当前声明/资格，不读数据，execute 时必须重新裁决。"""
    if plan['op'] != 'join':
        return dict(join_eliminable=False, reason='no join', non_binding=True)
    return dict(join_eliminable=_advisory_join(plan, catalog), non_binding=True)


def _advisory_join(join, catalog):
    if not join.get('_eliminable_shape', True):
        return False
    state = catalog.status()
    version = max((s['validated_state'] for s in state if s['validated_state'] is not None),
                  default=None)
    if version is None:
        return False
    evidence = catalog.join_evidence(
        join['kind'], join['left']['table'], join['left_key'],
        join['right']['table'], join['right_key'], version)
    if not evidence['complete']:
        return False
    if join['left']['op'] == 'join':
        return _advisory_join(join['left'], catalog)
    return True
