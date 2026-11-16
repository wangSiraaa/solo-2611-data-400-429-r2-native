"""端到端测试：目录资格、受控写入、运行时守卫与真实查询改写。

覆盖题面要求的全部小输入场景：
父重复、FK 列 NULL、LEFT 无匹配、未验证声明、验证失败后查询、
可信后非法写入拒绝、unchecked 导入后的下次 execute，以及两层 join、
源侧过滤、prepared 计划复用、输出身份排序与不去重。
"""

import copy
import json
import os
import unittest

import catalog
from catalog import Catalog, ConstraintViolation
import database
from database import Database
from planner import prepare
from executor import execute
from oracle import naive_execute

HERE = os.path.dirname(os.path.abspath(__file__))


def q_inner(proj=('child.row_id',)):
    return dict(op='join', kind='inner',
                left=dict(op='scan', table='child', projection=[]),
                right=dict(op='scan', table='parent', projection=[]),
                left_key='parent_id', right_key='id', projection=list(proj))


def q_left(proj=('child.row_id',)):
    q = q_inner(proj)
    q['kind'] = 'left'
    return q


def simple_tables(parent, child):
    return dict(parent=[dict(r) for r in parent], child=[dict(r) for r in child])


class EvidenceSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.catalog = Catalog()
        self.db.seed_unchecked('parent', [dict(row_id='p1', id=1), dict(row_id='p2', id=2)])
        self.db.seed_unchecked('child', [
            dict(row_id='c1', parent_id=1),
            dict(row_id='c2', parent_id=2),
            dict(row_id='c3', parent_id=1),  # 子侧重复合法
        ])
        for spec in (dict(kind='UNIQUE', table='parent', column='id'),
                     dict(kind='FOREIGN_KEY', table='child', column='parent_id',
                          parent='parent', parent_column='id'),
                     dict(kind='NOT_NULL', table='child', column='parent_id')):
            self.catalog.declare(spec)

    def test_01_declared_but_unvalidated_never_eliminates(self):
        prepared = prepare(q_inner(), self.catalog)
        result = execute(prepared, self.db, self.catalog)
        plan = json.dumps(result.physical_plan)
        self.assertIn('join', plan)
        self.assertNotIn('eliminated_join', plan)
        # explain 只是建议，不含任何消除承诺
        self.assertTrue(prepared['explain']['non_binding'])

    def test_02_validation_failure_keeps_join_and_records_witness(self):
        self.db.seed_unchecked('child', self.db.read_table('child') +
                               [dict(row_id='orphan', parent_id=99)])
        statuses = self.catalog.validate_all(self.db)
        fk = next(s for s in statuses if s['kind'] == 'FOREIGN_KEY')
        self.assertEqual(fk['status'], 'FAILED')
        self.assertIn('orphan', fk['witnesses'])
        result = execute(prepare(q_inner()), self.db, self.catalog)
        self.assertNotIn('eliminated_join', json.dumps(result.physical_plan))
        # 结果与朴素 oracle 一致：孤儿被 INNER 删除
        expected = naive_execute(q_inner(), simple_tables(
            self.db.read_table('parent'), self.db.read_table('child')))
        self.assertEqual(result.rows, expected)
        self.assertEqual(len(result.rows), 3)

    def test_03_trusted_catalog_eliminates_inner_and_skips_parent_reads(self):
        self.catalog.validate_all(self.db)
        counters = {}
        result = execute(prepare(q_inner()), self.db, self.catalog, counters=counters)
        self.assertEqual(result.physical_plan['op'], 'eliminated_join')
        self.assertEqual(result.rows, naive_execute(
            q_inner(), simple_tables(self.db.read_table('parent'),
                                     self.db.read_table('child'))))
        self.assertEqual(counters, {'child': 3})  # 父表业务行零读取
        self.assertEqual(result.access_trace, {'child': 3})

    def test_04_null_fk_column_blocks_inner_even_if_fk_passes(self):
        # SQL 式 FK 允许 NULL，因此 FK 验证通过；没有 NOT_NULL 证据时 INNER 不可消除
        self.db.seed_unchecked('child', self.db.read_table('child') +
                               [dict(row_id='cn', parent_id=None)])
        self.catalog.validate(dict(kind='FOREIGN_KEY', table='child', column='parent_id',
                                   parent='parent', parent_column='id'), self.db)
        self.catalog.validate(dict(kind='UNIQUE', table='parent', column='id'), self.db)
        nn = self.catalog.validate(dict(kind='NOT_NULL', table='child',
                                        column='parent_id'), self.db)
        self.assertEqual(nn['status'], 'FAILED')
        result = execute(prepare(q_inner()), self.db, self.catalog)
        self.assertNotIn('eliminated_join', json.dumps(result.physical_plan))
        expected = naive_execute(q_inner(), simple_tables(
            self.db.read_table('parent'), self.db.read_table('child')))
        self.assertEqual(result.rows, expected)  # NULL 行被 INNER 删掉
        self.assertEqual(len(result.rows), 3)
        # 但 LEFT 在同样数据下可经守卫消除：NULL 行在 LEFT 中本就保留
        left_res = execute(prepare(q_left()), self.db, self.catalog, strategy='guard')
        self.assertEqual(left_res.physical_plan['op'], 'guarded_join')
        self.assertEqual(len(left_res.rows), 4)

    def test_05_duplicate_parent_key_blocks_both_inner_and_left(self):
        self.db.seed_unchecked('parent', self.db.read_table('parent') +
                               [dict(row_id='p1dup', id=1)])
        self.catalog.validate_all(self.db)  # UNIQUE 验证失败
        result_i = execute(prepare(q_inner()), self.db, self.catalog, strategy='guard')
        result_l = execute(prepare(q_left()), self.db, self.catalog, strategy='guard')
        self.assertNotIn('eliminated', json.dumps(result_i.physical_plan))
        self.assertNotIn('eliminated', json.dumps(result_l.physical_plan))
        expected = naive_execute(q_inner(), simple_tables(
            self.db.read_table('parent'), self.db.read_table('child')))
        self.assertEqual(result_i.rows, expected)  # 父键 id=1 重复 → c1/c3 被放大
        # c1/c3 各匹配两个父行（4 行），c2 匹配一个（1 行），共 5 行
        self.assertEqual(len(result_i.rows), 5)
        # 目录路径同样拒绝消除：UNIQUE 资格为 FAILED
        cat_result = execute(prepare(q_inner()), self.db, self.catalog, strategy='catalog')
        self.assertEqual(cat_result.physical_plan['op'], 'join')

    def test_06_left_join_with_unmatched_child_eliminates_with_unique_only(self):
        # 没有任何 FK/NOT_NULL 证据；LEFT 只需父键 UNIQUE 即可消除
        db = Database()
        db.seed_unchecked('parent', [dict(row_id='p1', id=1)])
        db.seed_unchecked('child', [dict(row_id='c1', parent_id=1),
                                    dict(row_id='co', parent_id=7),   # 无匹配
                                    dict(row_id='cn', parent_id=None)])
        cat = Catalog()
        cat.declare_unique('parent', 'id')
        cat.validate(dict(kind='UNIQUE', table='parent', column='id'), db)
        counters = {}
        result = execute(prepare(q_left()), db, cat, counters=counters)
        self.assertEqual(result.physical_plan['op'], 'eliminated_join')
        self.assertEqual([r['child.row_id'] for r in result.rows],
                         ['c1', 'co', 'cn'])  # 无匹配与 NULL 行都保留
        self.assertEqual(counters, {'child': 3})

    def test_07_runtime_guard_path_eliminates_and_falls_back(self):
        # 干净数据 + 空目录：守卫读取父表后消除
        result = execute(prepare(q_inner()), self.db, Catalog(), strategy='guard')
        self.assertEqual(result.physical_plan['op'], 'guarded_join')
        self.assertEqual(result.access_trace, {'child': 3})
        self.assertEqual(self.db.ledger.report()['guard_rows'], {'parent': 2})
        # 注入孤儿后守卫失败 → fallback，父表进入 query 访问轨迹；
        # 守卫本身先读过一次左子树，故 child 计数为守卫读 + fallback 读 = 8
        self.db.seed_unchecked('child', self.db.read_table('child') +
                               [dict(row_id='orphan', parent_id=99)])
        result2 = execute(prepare(q_inner()), self.db, Catalog(), strategy='guard')
        self.assertEqual(result2.physical_plan['op'], 'join')
        self.assertEqual(result2.access_trace, {'child': 8, 'parent': 2})
        self.assertEqual(result2.rows, naive_execute(
            q_inner(), simple_tables(self.db.read_table('parent'),
                                     self.db.read_table('child'))))
        self.assertEqual(len(result2.rows), 3)

    def test_08_illegal_controlled_write_rejected_entirely(self):
        self.catalog.validate_all(self.db)
        good = dict(op='insert', table='child', row=dict(row_id='c9', parent_id=2))
        bad = dict(op='insert', table='child', row=dict(row_id='c10', parent_id=404))
        size_before = self.db.size('child')
        version_before = self.db.state_version
        with self.assertRaises(ConstraintViolation):
            self.db.apply(self.catalog, [good, bad])
        self.assertEqual(self.db.size('child'), size_before)      # 整体回滚
        self.assertEqual(self.db.state_version, version_before)  # 版本不动
        # 同批里的 good 也不可见
        self.assertNotIn('c9', [r['row_id'] for r in self.db.read_table('child')])
        # 合法批次正常提交，可信资格继续生效
        self.db.apply(self.catalog, [good])
        result = execute(prepare(q_inner()), self.db, self.catalog, strategy='catalog')
        self.assertEqual(result.physical_plan['op'], 'eliminated_join')

    def test_09_unchecked_import_atomically_downgrades_before_visibility(self):
        self.catalog.validate_all(self.db)
        report = self.db.unchecked_import(
            self.catalog, 'child',
            [dict(row_id='x1', parent_id=1), dict(row_id='x2', parent_id=None)])
        identities = {tuple(x) for x in report['downgraded_qualifications']}
        self.assertIn(('NOT_NULL', 'child', 'parent_id'), identities)
        self.assertIn(('FOREIGN_KEY', 'child', 'parent_id', 'parent', 'id'), identities)
        # UNIQUE(parent.id) 不覆盖 child，仍可信
        self.assertTrue(self.catalog.is_trusted('UNIQUE', 'parent', 'id'))
        # 下次 execute：INNER 不再被目录消除（auto 下会改走守卫，catalog 策略直接保留）
        result = execute(prepare(q_inner()), self.db, self.catalog, strategy='catalog')
        self.assertEqual(result.physical_plan['op'], 'join')
        self.assertEqual(result.rows, naive_execute(
            q_inner(), simple_tables(self.db.read_table('parent'),
                                     self.db.read_table('child'))))
        # 重新验证：NOT_NULL 因导入数据含 NULL 而失败，先修正数据再验证恢复消除
        clean_child = [dict(r, parent_id=1 if r['row_id'] == 'x2' else r['parent_id'])
                       for r in self.db.read_table('child')]
        self.db.unchecked_import(self.catalog, 'child', clean_child)
        self.catalog.validate_all(self.db)
        result2 = execute(prepare(q_inner()), self.db, self.catalog, strategy='catalog')
        self.assertEqual(result2.physical_plan['op'], 'eliminated_join')

    def test_10_prepared_plan_redecides_at_every_execute(self):
        prepared = prepare(q_inner())  # 准备时甚至没有目录
        r1 = execute(prepared, self.db, self.catalog, strategy='catalog')
        self.assertEqual(r1.physical_plan['op'], 'join')  # 无证据 → 保留
        self.catalog.validate_all(self.db)
        r2 = execute(prepared, self.db, self.catalog, strategy='catalog')  # 同一计划对象
        self.assertEqual(r2.physical_plan['op'], 'eliminated_join')
        self.db.unchecked_import(self.catalog, 'parent',
                                 self.db.read_table('parent'))
        r3 = execute(prepared, self.db, self.catalog, strategy='catalog')  # 降级后恢复 join
        self.assertEqual(r3.physical_plan['op'], 'join')

    def test_11_parent_side_filter_keeps_join(self):
        self.catalog.validate_all(self.db)
        query = dict(op='join', kind='inner',
                     left=dict(op='scan', table='child', projection=[]),
                     right=dict(op='scan', table='parent', projection=[],
                                filters=[dict(column='id', op='ge', value=2)]),
                     left_key='parent_id', right_key='id',
                     projection=['child.row_id'])
        result = execute(prepare(query), self.db, self.catalog)
        self.assertEqual(result.physical_plan['op'], 'join')
        self.assertEqual(result.rows, naive_execute(
            query, simple_tables(self.db.read_table('parent'),
                                 self.db.read_table('child'))))

    def test_12_source_side_filter_on_child_still_eliminates(self):
        self.catalog.validate_all(self.db)
        query = dict(op='join', kind='inner',
                     left=dict(op='scan', table='child', projection=[],
                               filters=[dict(column='parent_id', op='eq', value=1)]),
                     right=dict(op='scan', table='parent', projection=[]),
                     left_key='parent_id', right_key='id',
                     projection=['child.row_id'])
        result = execute(prepare(query), self.db, self.catalog)
        self.assertEqual(result.physical_plan['op'], 'eliminated_join')
        self.assertEqual([r['child.row_id'] for r in result.rows], ['c1', 'c3'])

    def test_13_output_identity_ordering_and_duplicates_preserved(self):
        # 两行完全相同的业务内容、不同 row_id：不得去重，按来源身份排序
        self.db.seed_unchecked('child', self.db.read_table('child') +
                               [dict(row_id='c4', parent_id=1),
                                dict(row_id='c5', parent_id=1)])
        self.catalog.validate_all(self.db)
        result = execute(prepare(q_inner()), self.db, self.catalog)
        ids = [r['child.row_id'] for r in result.rows]
        self.assertEqual(ids, ['c1', 'c2', 'c3', 'c4', 'c5'])
        self.assertEqual(len(ids), len(set(ids)))

    def test_14_two_level_join_elimination(self):
        db = Database()
        db.seed_unchecked('grandparent', [dict(row_id='g1', gid=1)])
        db.seed_unchecked('parent', [dict(row_id='p1', id=1, gid=1),
                                     dict(row_id='p2', id=2, gid=1)])
        db.seed_unchecked('child', [dict(row_id='c1', parent_id=1),
                                    dict(row_id='c2', parent_id=2)])
        cat = Catalog()
        for spec in (
            dict(kind='UNIQUE', table='grandparent', column='gid'),
            dict(kind='UNIQUE', table='parent', column='id'),
            dict(kind='NOT_NULL', table='parent', column='gid'),
            dict(kind='NOT_NULL', table='child', column='parent_id'),
            dict(kind='FOREIGN_KEY', table='parent', column='gid',
                 parent='grandparent', parent_column='gid'),
            dict(kind='FOREIGN_KEY', table='child', column='parent_id',
                 parent='parent', parent_column='id'),
        ):
            cat.declare(spec)
        cat.validate_all(db)
        inner1 = dict(op='join', kind='inner',
                      left=dict(op='scan', table='child', projection=[]),
                      right=dict(op='scan', table='parent', projection=[]),
                      left_key='parent_id', right_key='id', projection=[])
        query = dict(op='join', kind='inner', left=inner1,
                     right=dict(op='scan', table='grandparent', projection=[]),
                     left_key='parent.gid', right_key='gid',
                     projection=['child.row_id'])
        result = execute(prepare(query), db, cat)
        self.assertEqual(result.physical_plan['op'], 'eliminated_join')
        self.assertEqual(result.physical_plan['child']['op'], 'eliminated_join')
        self.assertEqual(result.access_trace, {'child': 2})
        expected = naive_execute(query, {t: db.read_table(t)
                                         for t in db.table_names()})
        self.assertEqual(result.rows, expected)


class AdversarialVariantTests(unittest.TestCase):
    """直接使用 clean-benchmark.json 的 variants：NULL 外键、父键重复。"""

    def setUp(self):
        with open(os.path.join(HERE, 'clean-benchmark.json'), encoding='utf-8') as fh:
            self.payload = json.load(fh)
        self.db = Database()
        self.db.seed_unchecked('parent', self.payload['parent'])
        self.db.seed_unchecked('child', self.payload['child'])
        self.cat = Catalog()
        for spec in self.payload['constraints']:
            self.cat.declare(spec)

    def _flat(self):
        return {t: self.db.read_table(t) for t in self.db.table_names()}

    def test_null_variant_fk_passes_but_not_null_fails(self):
        self.db.seed_unchecked('child', self.db.read_table('child') +
                               [self.payload['variants']['null']])
        statuses = {tuple(s['identity']): s['status']
                    for s in self.cat.validate_all(self.db)}
        self.assertEqual(statuses[('FOREIGN_KEY', 'child', 'parent_id',
                                   'parent', 'id')], 'TRUSTED')  # NULL 合法
        self.assertEqual(statuses[('NOT_NULL', 'child', 'parent_id')], 'FAILED')
        qi, ql = q_inner(), q_left()
        ri = execute(prepare(qi), self.db, self.cat, strategy='catalog')
        rl = execute(prepare(ql), self.db, self.cat, strategy='catalog')
        self.assertEqual(ri.physical_plan['op'], 'join')       # INNER 必须保留
        self.assertEqual(rl.physical_plan['op'], 'eliminated_join')  # LEFT 仅靠 UNIQUE
        self.assertEqual(ri.rows, naive_execute(qi, self._flat()))
        self.assertEqual(rl.rows, naive_execute(ql, self._flat()))

    def test_duplicate_parent_variant_invalidates_unique_and_guards(self):
        self.db.seed_unchecked('parent', self.db.read_table('parent') +
                               [self.payload['variants']['parent_duplicate']])
        statuses = {tuple(s['identity']): s['status']
                    for s in self.cat.validate_all(self.db)}
        self.assertEqual(statuses[('UNIQUE', 'parent', 'id')], 'FAILED')
        for strategy in ('catalog', 'guard'):
            for q in (q_inner(), q_left()):
                r = execute(prepare(q), self.db, self.cat, strategy=strategy)
                self.assertEqual(r.physical_plan['op'], 'join')
                self.assertEqual(r.rows, naive_execute(q, self._flat()))

    def test_two_level_left_elimination_needs_only_unique_each_level(self):
        db = Database()
        db.seed_unchecked('g', [dict(row_id='g1', gid=1)])
        db.seed_unchecked('p', [dict(row_id='p1', id=1, gid=1)])
        db.seed_unchecked('c', [dict(row_id='c1', pid=1),
                                dict(row_id='c2', pid=None)])  # NULL 对 LEFT 合法
        cat = Catalog()
        cat.declare_unique('g', 'gid')
        cat.declare_unique('p', 'id')
        cat.validate_all(db)
        level1 = dict(op='join', kind='left',
                      left=dict(op='scan', table='c', projection=[]),
                      right=dict(op='scan', table='p', projection=[]),
                      left_key='pid', right_key='id', projection=[])
        query = dict(op='join', kind='left', left=level1,
                     right=dict(op='scan', table='g', projection=[]),
                     left_key='p.gid', right_key='gid',
                     projection=['c.row_id'])
        result = execute(prepare(query), db, cat)
        self.assertEqual(result.physical_plan['op'], 'eliminated_join')
        self.assertEqual(result.access_trace, {'c': 2})
        self.assertEqual(result.rows,
                         naive_execute(query, {t: db.read_table(t)
                                               for t in db.table_names()}))
        self.assertEqual([r['c.row_id'] for r in result.rows], ['c1', 'c2'])


class CostLedgerTests(unittest.TestCase):
    def test_costs_kept_separate(self):
        db = Database()
        cat = Catalog()
        db.seed_unchecked('parent', [dict(row_id='p1', id=1)])
        db.seed_unchecked('child', [dict(row_id='c1', parent_id=1)])
        cat.declare_unique('parent', 'id')
        cat.declare_not_null('child', 'parent_id')
        cat.declare(dict(kind='FOREIGN_KEY', table='child', column='parent_id',
                         parent='parent', parent_column='id'))
        cat.validate_all(db)
        execute(prepare(q_inner()), db, cat)
        report = db.ledger.report()
        # 三个约束各读一次相关表：UNIQUE/NOT_NULL 各 1 次，FK 同时读两张表
        self.assertEqual(report['validation_reads'],
                         {'parent': 2, 'child': 2})
        self.assertEqual(report['query_rows'], {'child': 1})
        self.assertNotIn('parent', report['query_rows'])
        db.apply(cat, [dict(op='insert', table='child',
                            row=dict(row_id='c2', parent_id=1))])
        # 维护读取在提交前的暂存全表校验中产生
        self.assertIn('child', db.ledger.report()['maintenance_reads'])
        # 守卫成本独立桶
        execute(prepare(q_inner()), db, Catalog(), strategy='guard')
        self.assertEqual(db.ledger.report()['guard_rows'], {'parent': 1})


class LimitsAndGuardsTests(unittest.TestCase):
    def test_row_cap_and_batch_counters(self):
        import database
        db = Database()
        cat = Catalog()
        db.seed_unchecked('t', [dict(row_id='r0', v=1)])
        cat.declare_not_null('t', 'v')
        cat.validate_all(db)
        # 受控写入受每表行数上限约束（小批次、单行超出上限）
        overflow = [dict(row_id='r%d' % i, v=i)
                    for i in range(database.MAX_ROWS_PER_TABLE)]
        db.unchecked_import(cat, 't', overflow)
        with self.assertRaises(RuntimeError):
            db.apply(cat, [dict(op='insert', table='t',
                                row=dict(row_id='one_too_many', v=1))])
        # 被拒批次计入 rejected_batches，成功批次计入 applied_batches
        self.assertEqual(db.ledger.report()['rejected_batches'], {'#': 1})
        self.assertEqual(db.ledger.report()['applied_batches'], {})
        # 合法小批次成功并计数
        db2 = Database()
        c2 = Catalog()
        db2.seed_unchecked('t', [dict(row_id='a', v=1)])
        c2.declare_not_null('t', 'v')
        c2.validate_all(db2)
        db2.apply(c2, [dict(op='delete', table='t', row_id='a')])
        self.assertEqual(db2.ledger.report()['applied_batches'], {'#': 1})

    def test_declare_idempotent_and_capped(self):
        cat = Catalog()
        for i in range(catalog.MAX_CONSTRAINTS):
            cat.declare_not_null('t', 'c%d' % i)
        with self.assertRaises(RuntimeError):
            cat.declare_not_null('t', 'extra')
        before = len(cat.identities())
        cat.declare_not_null('t', 'c0')  # 重复声明幂等
        self.assertEqual(len(cat.identities()), before)

    def test_guard_respects_left_side_filter(self):
        db = Database()
        db.seed_unchecked('parent', [dict(row_id='p1', id=1), dict(row_id='p2', id=2)])
        # 孤儿只存在于被过滤掉的行里；过滤后守卫应通过并消除
        db.seed_unchecked('child', [dict(row_id='c1', parent_id=1),
                                    dict(row_id='c2', parent_id=2),
                                    dict(row_id='orphan', parent_id=99)])
        query = dict(op='join', kind='inner',
                     left=dict(op='scan', table='child', projection=[],
                               filters=[dict(column='parent_id', op='le', value=2)]),
                     right=dict(op='scan', table='parent', projection=[]),
                     left_key='parent_id', right_key='id',
                     projection=['child.row_id'])
        result = execute(prepare(query), db, Catalog(), strategy='guard')
        self.assertEqual(result.physical_plan['op'], 'guarded_join')
        self.assertEqual([r['child.row_id'] for r in result.rows], ['c1', 'c2'])
        # 不加过滤时同一数据守卫必须失败并 fallback
        full = dict(query)
        full['left'] = dict(op='scan', table='child', projection=[])
        fallback = execute(prepare(full), db, Catalog(), strategy='guard')
        self.assertEqual(fallback.physical_plan['op'], 'join')
        self.assertEqual(fallback.rows, naive_execute(
            full, {'parent': db.read_table('parent'),
                   'child': db.read_table('child')}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
