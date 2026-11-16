"""随机化属性测试：任何策略下，执行结果必须与朴素 oracle 完全一致。

随机生成含 NULL、重复键、孤儿、父键重复的小关系，随机声明/验证约束，
对 INNER/LEFT、源侧过滤、两层 join 全部查询，比较 executor 与 oracle 的
有序输出（含重复出现），并核对消除时父表业务行确实零读取。
"""

import random
import unittest

from catalog import Catalog
from database import Database
from planner import prepare
from executor import execute
from oracle import naive_execute


def build_world(rng):
    db = Database()
    n_parent = rng.randint(1, 8)
    # 父键池允许重复与 None，刻意构造脏数据
    id_pool = [rng.randint(0, 5) for _ in range(n_parent)]
    if rng.random() < 0.3:
        id_pool.append(id_pool[0])  # 制造父键重复
    parents = [dict(row_id='p%d' % i, id=v) for i, v in enumerate(id_pool)]
    db.seed_unchecked('parent', parents)

    n_child = rng.randint(1, 12)
    children = []
    for i in range(n_child):
        roll = rng.random()
        if roll < 0.2:
            pid = None                       # FK 列 NULL
        elif roll < 0.4:
            pid = rng.randint(90, 99)        # 孤儿
        else:
            pid = rng.choice(id_pool)
        children.append(dict(row_id='c%d' % i, parent_id=pid, value=rng.randint(0, 20)))
    db.seed_unchecked('child', children)

    cat = Catalog()
    cat.declare_unique('parent', 'id')
    cat.declare_not_null('child', 'parent_id')
    cat.declare(dict(kind='FOREIGN_KEY', table='child', column='parent_id',
                     parent='parent', parent_column='id'))
    if rng.random() < 0.8:
        cat.validate_all(db)
    return db, cat, parents, children


def join_query(kind, filters=None):
    return dict(op='join', kind=kind,
                left=dict(op='scan', table='child', projection=[],
                          filters=filters or []),
                right=dict(op='scan', table='parent', projection=[]),
                left_key='parent_id', right_key='id',
                projection=['child.row_id'])


def _flt(row, f):
    ops = {'eq': lambda a, b: a == b, 'ne': lambda a, b: a != b,
           'lt': lambda a, b: a < b, 'le': lambda a, b: a <= b,
           'gt': lambda a, b: a > b, 'ge': lambda a, b: a >= b}
    val = row.get(f['column'])
    return val is not None and ops[f['op']](val, f['value'])


class PropertyTests(unittest.TestCase):
    def test_random_worlds_match_oracle_every_strategy(self):
        rng = random.Random(20261116)
        strategies = ['naive', 'catalog', 'guard', 'auto']
        for trial in range(300):
            db, cat, parents, children = build_world(rng)
            flat = dict(parent=parents, child=children)
            queries = [join_query('inner'), join_query('left'),
                       join_query('inner', [dict(column='value', op='ge', value=5)]),
                       join_query('left', [dict(column='parent_id', op='eq', value=1)])]
            for query in queries:
                expected = naive_execute(query, flat)
                # 朴素扫描基准：任何“消除 join”的正确结果都必须与过滤后的 child 扫描同长
                scan_only = [dict(row_id=r['row_id']) for r in children
                             if all(_flt(r, f) for f in (query['left'].get('filters') or []))]
                for strategy in strategies:
                    result = execute(prepare(query), db, cat, strategy=strategy)
                    self.assertEqual(
                        result.rows, expected,
                        msg='trial=%d strategy=%s query=%s' % (trial, strategy, query))
                    phys = result.physical_plan
                    # 消除成立时父表不得出现在 query 访问轨迹，且结果等价于 child 扫描
                    if phys['op'] in ('eliminated_join', 'guarded_join'):
                        self.assertNotIn('parent', result.access_trace)
                        self.assertEqual(len(result.rows), len(scan_only))
                        self.assertEqual([r['child.row_id'] for r in result.rows],
                                         [r['row_id'] for r in scan_only])
                    elif phys['op'] == 'join':
                        self.assertIn('parent', result.access_trace)

    def test_random_mutations_revalidate_cycle(self):
        rng = random.Random(424242)
        db, cat, _, _ = build_world(rng)
        query = join_query('inner')
        for _ in range(100):
            flat = {t: db.read_table(t) for t in db.table_names()}
            expected = naive_execute(query, flat)
            for strategy in ('catalog', 'guard', 'auto', 'naive'):
                result = execute(prepare(query), db, cat, strategy=strategy)
                self.assertEqual(result.rows, expected)
            # 随机受控写入；非法批次预期被拒，库状态不变
            pid = rng.choice([None, 1, 99])
            op = dict(op='insert', table='child',
                      row=dict(row_id='new%d' % rng.randint(0, 10 ** 6), parent_id=pid))
            from catalog import ConstraintViolation
            try:
                db.apply(cat, [op])
            except ConstraintViolation:
                pass
            if rng.random() < 0.2:
                db.unchecked_import(cat, 'parent', db.read_table('parent'))
            if rng.random() < 0.3:
                cat.validate_all(db)


if __name__ == '__main__':
    unittest.main(verbosity=2)
