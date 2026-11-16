"""有限 JSON 行 bag 存储：独立 row_id、版本号、受控 apply 与 unchecked_import。

* 每个关系是保留重复出现的行 bag，行按插入顺序获得内部序号；
* state_version 在每次可查询数据变化后 +1，目录资格按版本解释；
* apply() 只走受控写路径，批次在暂存表上整体校验所有 TRUSTED 约束，
  冲突时整体拒绝（不提交、不升版本）；
* unchecked_import() 不经任何约束检查，数据落地前先原子降级覆盖相关表的资格。
"""

from catalog import ConstraintViolation

MAX_TABLES = 3
MAX_ROWS_PER_TABLE = 1000
MAX_BATCH_OPS = 100
MAX_CHANGE_BATCHES = 100


class CostLedger:
    """把不同目的的行读取分开记账，绝不混进笼统的“元数据”。

    query_rows          查询算子真正读取的业务行（被消除的父表不会出现）；
    guard_rows          运行时守卫为决定能否消除而读取的行；
    validation_reads    catalog.validate 的全量校验读取；
    maintenance_reads   受控 apply 为维护可信约束的暂存表读取；
    rejected_batches    被整体拒绝的批次数；
    applied_batches     成功提交的批次数；
    unchecked_imports   非受控导入次数。
    """

    BUCKETS = ('query_rows', 'guard_rows', 'validation_reads',
               'maintenance_reads', 'rejected_batches',
               'applied_batches', 'unchecked_imports')

    def __init__(self):
        self._counters = {b: {} for b in self.BUCKETS}

    def add(self, bucket, table, n):
        if bucket not in self._counters:
            raise KeyError(bucket)
        self._counters[bucket][table] = self._counters[bucket].get(table, 0) + n

    def add_scalar(self, bucket, n=1):
        self._counters[bucket]['#'] = self._counters[bucket].get('#', 0) + n

    def report(self):
        return {b: dict(v) for b, v in self._counters.items()}


class Database:
    def __init__(self, ledger=None):
        self._tables = {}
        self._state_version = 0
        self._seq = 0
        self._change_batches = 0
        self.ledger = ledger if ledger is not None else CostLedger()

    # ---------- 只读访问 ----------

    @property
    def state_version(self):
        return self._state_version

    def table_names(self):
        return list(self._tables)

    def size(self, table):
        return len(self._tables[table])

    def read_table(self, table):
        """返回业务行副本（不含内部序号），目录校验用。"""
        return [dict(row['data']) for row in self._tables[table]]

    def _scan(self, table):
        """内部：带序号的行，序号即来源出现身份，排序与访问计数用。"""
        return list(self._tables[table])

    # ---------- 建表与受控批次 ----------

    def create_table(self, name):
        if name not in self._tables:
            if len(self._tables) >= MAX_TABLES:
                raise RuntimeError('at most %d tables allowed' % MAX_TABLES)
            self._tables[name] = []
        return name

    def seed_unchecked(self, name, rows):
        """装载初始数据的非受控入口（benchmark 装载用），同样降级目录。"""
        self.create_table(name)
        self._install(name, rows)

    def apply(self, catalog, ops):
        """受控批次：ops 为 [{'op': insert|update|delete, 'table':..., ...}]。

        先在内存暂存表上应用整批，再用可信目录整体校验；任一冲突则全部拒绝。
        """
        if len(ops) > MAX_BATCH_OPS:
            raise RuntimeError('at most %d ops per batch' % MAX_BATCH_OPS)
        if self._change_batches >= MAX_CHANGE_BATCHES:
            raise RuntimeError('at most %d change batches allowed' % MAX_CHANGE_BATCHES)
        for op in ops:
            self._check_table(op['table'])
        before = {t: [dict(r['data']) for r in rows]
                  for t, rows in self._tables.items()}
        staging = {t: [dict(r) for r in rows] for t, rows in before.items()}
        try:
            for op in ops:
                self._stage_op(staging, op)
            for t, rows in staging.items():
                if len(rows) > MAX_ROWS_PER_TABLE:
                    raise RuntimeError('table %r exceeds %d rows'
                                       % (t, MAX_ROWS_PER_TABLE))
            catalog.enforce_trusted(staging, self, self.ledger)
        except (ConstraintViolation, RuntimeError, KeyError, ValueError):
            # 任何提交前失败都整体拒绝：暂存表丢弃，正式数据与版本不变
            self.ledger.add_scalar('rejected_batches')
            raise
        for table, rows in staging.items():
            self._tables[table] = [
                dict(seq=self._next_seq(), data=dict(row)) for row in rows]
        self._state_version += 1
        self._change_batches += 1
        catalog.extend_trusted(
            {t: len(r) for t, r in staging.items()}, self._state_version)
        self.ledger.add_scalar('applied_batches')
        return dict(committed=True, state_version=self._state_version,
                    sizes={t: len(r) for t, r in self._tables.items()})

    def unchecked_import(self, catalog, table, rows):
        """显式非受控导入：任何约束都不检查。

        数据可查询之前，先原子撤销所有覆盖该表的可信资格；随后才安装数据并升版本。
        """
        self._check_table(table)
        if len(rows) > MAX_ROWS_PER_TABLE:
            raise RuntimeError('table %r exceeds %d rows' % (table, MAX_ROWS_PER_TABLE))
        downgraded = catalog.invalidate_for(table)   # 先降级，后可见
        self._install(table, rows)
        self._state_version += 1
        self._change_batches += 1
        self.ledger.add_scalar('unchecked_imports')
        return dict(imported=len(rows), state_version=self._state_version,
                    downgraded_qualifications=downgraded)

    # ---------- 内部实现 ----------

    def _install(self, table, rows):
        for row in rows:
            if 'row_id' not in row:
                raise ValueError('every row must carry row_id')
        self._tables[table] = [dict(seq=self._next_seq(), data=dict(row))
                               for row in rows]

    def _next_seq(self):
        self._seq += 1
        return self._seq

    def _check_table(self, table):
        if table not in self._tables:
            raise KeyError('unknown table: %r' % table)

    def _stage_op(self, staging, op):
        kind = op['op']
        table = op['table']
        rows = staging[table]
        if kind == 'insert':
            row = dict(op['row'])
            if 'row_id' not in row:
                raise ValueError('every row must carry row_id')
            rows.append(row)
        elif kind == 'delete':
            target = op['row_id']
            kept = [r for r in rows if r['row_id'] != target]
            if len(kept) == len(rows):
                raise KeyError('row_id not found: %r' % target)
            staging[table] = kept
        elif kind == 'update':
            target = op['row_id']
            found = False
            for r in rows:
                if r['row_id'] == target:
                    r.update(dict(op['set']))
                    found = True
                    break
            if not found:
                raise KeyError('row_id not found: %r' % target)
        else:
            raise ValueError('unknown op: %r' % kind)
