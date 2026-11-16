"""独立小输入 oracle：不依赖 catalog / 优化器，朴素执行同一 JSON 查询。

语义与 executor 的保留 join 路径一致，但实现独立，供属性测试逐查询对照：
  * 等值连接 NULL 不匹配；
  * INNER 删孤儿/NULL 子行，LEFT 无匹配保留左行；
  * 父键重复按匹配数放大，保留 bag 重复；
  * 输出按来源出现身份（行插入序号链）排序，不去重。
"""


def naive_execute(query, tables):
    """tables: {table: [普通 dict 业务行]}，行序即来源出现身份。"""
    node = query if isinstance(query, dict) and 'op' in query else query
    drows = _eval(node, tables)
    drows.sort(key=lambda d: d[0])
    return [_project(d[1], node['projection']) for d in drows]


def _eval(node, tables):
    if node['op'] == 'scan':
        out = []
        for idx, row in enumerate(tables[node['table']]):
            if all(_match(row, f) for f in node.get('filters', [])):
                out.append(((idx,), {node['table']: dict(row)}))
        return out
    left_rows = _eval(node['left'], tables)
    right_rows = _eval(node['right'], tables)
    lk_table, lk_col = _split_key(node['left_key'])
    out = []
    for lprov, lbag in left_rows:
        lval = _get(lbag, lk_table, lk_col)
        matches = [(rprov, rbag) for rprov, rbag in right_rows
                   if lval is not None
                   and _get(rbag, node['right']['table'], node['right_key']) is not None
                   and lval == _get(rbag, node['right']['table'], node['right_key'])]
        if not matches and node['kind'] == 'left':
            out.append((lprov + (-1,), dict(lbag)))
            continue
        for rprov, rbag in matches:
            merged = dict(lbag)
            merged.update(rbag)
            out.append((lprov + rprov, merged))
    return out


def _split_key(key):
    if '.' in key:
        return tuple(key.split('.', 1))
    return None, key


def _match(row, flt):
    ops = {'eq': lambda a, b: a == b, 'ne': lambda a, b: a != b,
           'lt': lambda a, b: a < b, 'le': lambda a, b: a <= b,
           'gt': lambda a, b: a > b, 'ge': lambda a, b: a >= b}
    val = row.get(flt['column'])
    return val is not None and ops[flt['op']](val, flt['value'])


def _get(bag, table, column):
    if table is not None:
        return bag.get(table, {}).get(column)
    for row in bag.values():
        if column in row:
            return row[column]
    return None


def _project(bag, projection):
    out = {}
    for col in projection:
        if '.' in col:
            table, column = col.split('.', 1)
            out[col] = bag.get(table, {}).get(column)
        else:
            out[col] = _get(bag, None, col)
    return out
