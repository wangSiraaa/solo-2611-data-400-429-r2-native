"""公开 clean 基准驱动：验证约束 → 实际消除 INNER 与 LEFT → 交付三份证据。

运行：
    python3 benchmark.py            # 打印摘要
    python3 benchmark.py --json out.json

交付物：
  1. catalog_qualifications 目录资格记录（状态/覆盖范围/计数/见证/源版本）；
  2. physical_plans          实际算子计划（消除或保留，含依赖前提）；
  3. access_trace            真实访问计数，被消除父表在 query 阶段零业务行读取；
  4. cost_ledger             先前验证、守卫读取、维护写入成本分桶如实记录。
"""

import json
import os

from catalog import Catalog, ConstraintViolation
from database import Database
from planner import prepare
from executor import execute
from oracle import naive_execute

HERE = os.path.dirname(os.path.abspath(__file__))


def load_clean(db):
    with open(os.path.join(HERE, 'clean-benchmark.json'), encoding='utf-8') as fh:
        payload = json.load(fh)
    db.seed_unchecked('parent', payload['parent'])
    db.seed_unchecked('child', payload['child'])
    cat = Catalog()
    for spec in payload['constraints']:
        cat.declare(spec)
    return cat, payload['variants']


def inner_query():
    return dict(op='join', kind='inner',
                left=dict(op='scan', table='child', projection=[]),
                right=dict(op='scan', table='parent', projection=[]),
                left_key='parent_id', right_key='id',
                projection=['child.row_id'])


def left_query():
    q = inner_query()
    q['kind'] = 'left'
    return q


def main():
    db = Database()
    catalog, variants = load_clean(db)

    before_validation = catalog.status()
    catalog.validate_all(db)                       # 先前验证成本计入 validation_reads
    qualifications = catalog.status()

    # —— 同一源状态下：INNER 与 LEFT 各实际消除一次 ——
    prepared_inner = prepare(inner_query(), catalog)
    prepared_left = prepare(left_query(), catalog)
    inner_res = execute(prepared_inner, db, catalog, strategy='catalog')
    left_res = execute(prepared_left, db, catalog, strategy='catalog')

    flat = {'parent': db.read_table('parent'), 'child': db.read_table('child')}
    assert inner_res.rows == naive_execute(inner_query(), flat)
    assert left_res.rows == naive_execute(left_query(), flat)
    assert 'parent' not in inner_res.access_trace
    assert 'parent' not in left_res.access_trace

    # —— 守卫路径演示：空目录也能靠运行时守卫消除 ——
    guard_res = execute(prepare(inner_query()), db, Catalog(), strategy='guard')
    assert guard_res.physical_plan['op'] == 'guarded_join'

    # —— 受控写入：非法批次整体拒绝（更新为不存在的父键，违反可信 FK）——
    illegal = [dict(op='update', table='child', row_id='c0',
                    set=dict(parent_id=404))]
    rejected = None
    try:
        db.apply(catalog, illegal)
    except ConstraintViolation as exc:
        rejected = dict(constraint=exc.constraint['identity'],
                        counts=exc.counts, witnesses=exc.witnesses)
    assert rejected is not None

    # —— 非受控导入：原子降级后，下次 execute 保留 join ——
    # 用孤儿变体替换一行，保持每表 ≤1000 行的限制
    dirty_children = db.read_table('child')
    dirty_children[0] = dict(row_id=variants['orphan']['row_id'],
                             parent_id=variants['orphan']['parent_id'],
                             value=dirty_children[0].get('value'))
    downgrade = db.unchecked_import(catalog, 'child', dirty_children)
    after_import = execute(prepare(inner_query()), db, catalog, strategy='catalog')
    assert after_import.physical_plan['op'] == 'join'

    report = dict(
        source_state=dict(version=db.state_version,
                          sizes={t: db.size(t) for t in db.table_names()}),
        catalog_qualifications=dict(
            before_validation=before_validation,
            after_validation=qualifications,
            after_unchecked_import=catalog.status()),
        elimination=dict(
            inner=dict(physical_plan=inner_res.physical_plan,
                       access_trace=inner_res.access_trace,
                       rows_returned=len(inner_res.rows),
                       parent_business_rows_read=0),
            left=dict(physical_plan=left_res.physical_plan,
                      access_trace=left_res.access_trace,
                      rows_returned=len(left_res.rows),
                      parent_business_rows_read=0),
            runtime_guard=dict(physical_plan=guard_res.physical_plan,
                               access_trace=guard_res.access_trace)),
        controlled_write=dict(rejected_batch=rejected),
        unchecked_import=dict(report=downgrade,
                              next_execute_plan=after_import.physical_plan['op']),
        cost_ledger=db.ledger.report(),
        prerequisite_explanation=dict(
            INNER=['FOREIGN_KEY(child.parent_id->parent.id) 保证非NULL子键必有父，'
                   '否则孤儿/NULL子行会被INNER删除',
                   'NOT_NULL(child.parent_id) 保证子键无NULL，SQL等值连接NULL不匹配',
                   'UNIQUE(parent.id) 保证每个子键至多一个父匹配，否则父重复放大行数'],
            LEFT=['仅需 UNIQUE(parent.id)：LEFT无匹配时本就保留左行，'
                  'NULL子键与无匹配子键都不会被丢弃']),
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(_summary(report))
    if '--json' in os.sys.argv:
        out = os.sys.argv[os.sys.argv.index('--json') + 1]
        with open(out, 'w', encoding='utf-8') as fh:
            fh.write(text)
        print('written', out)
    return report


def _summary(report):
    e = report['elimination']
    c = report['cost_ledger']
    lines = [
        '源状态版本: %s 表规模: %s' % (report['source_state']['version'],
                                    report['source_state']['sizes']),
        '目录资格: %s' % [(q['identity'], q['status'])
                         for q in report['catalog_qualifications']['after_validation']],
        'INNER 实际算子: %s；访问轨迹: %s（父表业务行读取=%d）'
        % (e['inner']['physical_plan']['op'], e['inner']['access_trace'],
           e['inner']['parent_business_rows_read']),
        'LEFT  实际算子: %s；访问轨迹: %s（父表业务行读取=%d）'
        % (e['left']['physical_plan']['op'], e['left']['access_trace'],
           e['left']['parent_business_rows_read']),
        '守卫路径算子: %s；访问轨迹: %s' % (e['runtime_guard']['physical_plan']['op'],
                                          e['runtime_guard']['access_trace']),
        '非法受控批次: %s' % report['controlled_write']['rejected_batch'],
        'unchecked 导入降级: %s；下次 execute 算子: %s'
        % (report['unchecked_import']['report']['downgraded_qualifications'],
           report['unchecked_import']['next_execute_plan']),
        '成本分桶: validation=%s guard=%s maintenance=%s query=%s rejected=%s'
        % (c['validation_reads'], c['guard_rows'], c['maintenance_reads'],
           c['query_rows'], c['rejected_batches']),
    ]
    return '\n'.join(lines)


if __name__ == '__main__':
    main()
