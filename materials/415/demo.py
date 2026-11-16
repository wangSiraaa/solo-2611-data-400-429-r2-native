"""演示：声明不等于可信；连接消除必须由目录资格、受控写入与真实改写共同决定。

第一段：完整父子关系 + 完成 validate 后，INNER 与 LEFT 都能安全消除，
且执行期不读取父表业务行，结果与朴素 oracle 完全一致。
第二段：同一查询只 declare 不 validate，并加入孤儿子行，
朴素 join（oracle）删除孤儿，而盲删会多返回一行 —— 因此默认必须保留 join。
"""

import json

from catalog import Catalog
from database import Database
from planner import prepare
from executor import execute
from oracle import naive_execute


def query(kind):
    return dict(op='join', kind=kind,
                left=dict(op='scan', table='child', projection=[]),
                right=dict(op='scan', table='parent', projection=[]),
                left_key='parent_id', right_key='id',
                projection=['row_id'])


def main():
    db = Database()
    db.seed_unchecked('parent', [dict(row_id='p', id=1)])
    db.seed_unchecked('child', [dict(row_id='ok', parent_id=1)])
    catalog = Catalog()
    catalog.declare_fk('child', 'parent_id', 'parent', 'id')
    catalog.declare_unique('parent', 'id')
    catalog.declare_not_null('child', 'parent_id')

    flat = {'parent': db.read_table('parent'), 'child': db.read_table('child')}

    # 未验证：即使数据恰好干净，也只能保留 join
    unverified = execute(prepare(query('inner')), db, catalog, strategy='catalog')
    unverified_ok = unverified.physical_plan['op'] == 'join'

    # 完成真实关系校验后，资格与当前源状态绑定
    catalog.validate_all(db)
    inner_res = execute(prepare(query('inner')), db, catalog, strategy='catalog')
    left_res = execute(prepare(query('left')), db, catalog, strategy='catalog')
    normal = (inner_res.rows == naive_execute(query('inner'), flat)
              and left_res.rows == naive_execute(query('left'), flat)
              and inner_res.physical_plan['op'] == 'eliminated_join'
              and left_res.physical_plan['op'] == 'eliminated_join'
              and 'parent' not in inner_res.access_trace
              and 'parent' not in left_res.access_trace)

    # 反例：只 declare、未 validate 的目录 + 孤儿子行
    dirty_db = Database()
    dirty_db.seed_unchecked('parent', [dict(row_id='p', id=1)])
    dirty_db.seed_unchecked('child', [dict(row_id='ok', parent_id=1),
                                      dict(row_id='orphan', parent_id=2)])
    blind_catalog = Catalog()
    blind_catalog.declare_fk('child', 'parent_id', 'parent', 'id')
    safe = execute(prepare(query('inner')), dirty_db, blind_catalog,
                   strategy='catalog')  # 证据不足 → 保留真实 join
    expected = naive_execute(
        query('inner'),
        {'parent': dirty_db.read_table('parent'),
         'child': dirty_db.read_table('child')})
    blind_scan = [{'row_id': r['row_id']} for r in dirty_db.read_table('child')]

    print(json.dumps(dict(
        unverified_declaration_keeps_join=unverified_ok,
        normal_path_ok=normal,
        normal_observation='完整父子关系且三类证据同版本可信时，INNER/LEFT 均消除，'
                           '执行期父表业务行零读取，结果与朴素 oracle 一致。',
        inner_access_trace=inner_res.access_trace,
        left_access_trace=left_res.access_trace,
        counterexample_detected=blind_scan != expected,
        safe_actual=safe.rows,
        blind_elimination_would_return=blind_scan,
        expected_naive_join=expected,
        failure_observation='未验证 FK 下存在孤儿行：盲删 INNER 会返回 ok 与 orphan 共两行，'
                           '朴素 SQL 式等值 join 只返回 ok；因此无证据时保留 join。',
        oracle_basis='对真实父子 bag 逐对执行非 NULL 等值，保留匹配出现次数，按来源身份排序。',
    ), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
