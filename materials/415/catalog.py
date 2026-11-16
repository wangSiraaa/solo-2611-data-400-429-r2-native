"""约束目录：declare / validate / status。

declare 只是“待验证主张”（DECLARED）；validate 必须读取真实关系，把覆盖范围、
违反见证和统计计数落到资格记录上，验证通过才是 TRUSTED，验证失败是 FAILED。
三类证据互相独立：

* UNIQUE      —— 父键非 NULL 值互不重复（决定 join 是否会放大行数）；
* FOREIGN_KEY —— SQL 式引用存在性，允许子侧外键列为 NULL，NULL 行不参与校验；
* NOT_NULL    —— 外键列没有 NULL（FK 验证本身允许 NULL，不能替代 NOT_NULL）。

资格记录带有所验证数据源的 state_version；数据一旦经过非受控路径变化，
相关资格必须被原子降级（见 database.Database.unchecked_import）。
"""

MAX_CONSTRAINTS = 10
WITNESS_CAP = 5


class ConstraintViolation(Exception):
    """受控写入批次违反可信约束时抛出，整个批次必须被拒绝。"""

    def __init__(self, constraint, witnesses, counts):
        self.constraint = constraint
        self.witnesses = witnesses
        self.counts = counts
        super().__init__('constraint %s violated: %s' % (constraint['identity'], counts))


def _identity(spec):
    kind = spec['kind']
    if kind == 'FOREIGN_KEY':
        return (kind, spec['table'], spec['column'],
                spec['parent'], spec['parent_column'])
    return (kind, spec['table'], spec['column'])


def _normalize(spec):
    kind = spec['kind']
    rec = {'kind': kind, 'table': spec['table'], 'column': spec['column']}
    if kind == 'FOREIGN_KEY':
        rec['parent'] = spec['parent']
        rec['parent_column'] = spec['parent_column']
    if kind not in ('UNIQUE', 'FOREIGN_KEY', 'NOT_NULL'):
        raise ValueError('unknown constraint kind: %r' % kind)
    rec['identity'] = list(_identity(rec))
    return rec


# ---- 纯函数式检查：validate 与受控写入维护共用同一套语义 ----

def check_not_null(rows, column):
    """返回 (违反行数, 见证 row_id 列表)。"""
    witnesses = []
    nulls = 0
    for row in rows:
        if row.get(column) is None:
            nulls += 1
            if len(witnesses) < WITNESS_CAP:
                witnesses.append(row.get('row_id'))
    return nulls, witnesses


def check_unique(rows, column):
    """只检查非 NULL 值；返回 (重复值个数, 重复见证 row_id, 涉及行数)。"""
    seen = {}
    duplicate_values = set()
    witnesses = []
    for row in rows:
        value = row.get(column)
        if value is None:
            continue
        key = _json_key(value)
        if key in seen:
            duplicate_values.add(key)
            if len(witnesses) < WITNESS_CAP:
                witnesses.append(row.get('row_id'))
        else:
            seen[key] = row.get('row_id')
    return len(duplicate_values), witnesses


def check_fk(child_rows, child_column, parent_rows, parent_column):
    """SQL 式外键检查：子侧 NULL 合法；非 NULL 值必须在父侧非 NULL 键中存在。"""
    parent_keys = set()
    for row in parent_rows:
        value = row.get(parent_column)
        if value is not None:
            parent_keys.add(_json_key(value))
    missing = 0
    null_children = 0
    witnesses = []
    for row in child_rows:
        value = row.get(child_column)
        if value is None:
            null_children += 1
            continue
        if _json_key(value) not in parent_keys:
            missing += 1
            if len(witnesses) < WITNESS_CAP:
                witnesses.append(row.get('row_id'))
    return missing, null_children, witnesses


def _json_key(value):
    """让 list/dict 型 JSON 值也能进 set，同时区分 bool 与 int。"""
    if isinstance(value, (list, dict)):
        return ('json', _stable(value))
    if isinstance(value, bool):
        return ('bool', value)
    return (type(value).__name__, value)


def _stable(value):
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class Catalog:
    def __init__(self):
        self._constraints = {}
        self._validation_seq = 0

    # ---------- declare：登记主张，不读数据，不产生信任 ----------

    def declare(self, spec):
        rec = _normalize(spec)
        key = tuple(rec['identity'])
        if key not in self._constraints:
            if len(self._constraints) >= MAX_CONSTRAINTS:
                raise RuntimeError('at most %d constraints allowed' % MAX_CONSTRAINTS)
            rec.update(status='DECLARED', validated_state=None, validated_seq=None,
                       coverage=None, witnesses=[], counts=None)
            self._constraints[key] = rec
        return self.status(key)

    def declare_unique(self, table, column):
        return self.declare(dict(kind='UNIQUE', table=table, column=column))

    def declare_not_null(self, table, column):
        return self.declare(dict(kind='NOT_NULL', table=table, column=column))

    def declare_fk(self, child, column, parent, parent_column):
        return self.declare(dict(kind='FOREIGN_KEY', table=child, column=column,
                                 parent=parent, parent_column=parent_column))

    # ---------- validate：读真实关系，保存覆盖范围/见证/资格 ----------

    def validate(self, spec, db, ledger=None):
        key = tuple(_normalize(spec)['identity']) if isinstance(spec, dict) else tuple(spec)
        rec = self._constraints.get(key)
        if rec is None:
            raise KeyError('constraint not declared: %r' % (spec,))
        if ledger is None:
            ledger = db.ledger
        state = db.state_version
        coverage = {}
        if rec['kind'] == 'NOT_NULL':
            rows = db.read_table(rec['table'])
            coverage[rec['table']] = len(rows)
            nulls, witnesses = check_not_null(rows, rec['column'])
            counts = dict(checked=len(rows), null_values=nulls)
            ok = nulls == 0
        elif rec['kind'] == 'UNIQUE':
            rows = db.read_table(rec['table'])
            coverage[rec['table']] = len(rows)
            dupes, witnesses = check_unique(rows, rec['column'])
            counts = dict(checked=len(rows), duplicate_values=dupes)
            ok = dupes == 0
        else:
            child_rows = db.read_table(rec['table'])
            parent_rows = db.read_table(rec['parent'])
            coverage[rec['table']] = len(child_rows)
            coverage[rec['parent']] = len(parent_rows)
            missing, null_children, witnesses = check_fk(
                child_rows, rec['column'], parent_rows, rec['parent_column'])
            counts = dict(checked_child=len(child_rows), checked_parent=len(parent_rows),
                          missing_references=missing, null_fk_rows=null_children)
            ok = missing == 0  # NULL 行合法，不致使 FK 验证失败
        if ledger is not None:
            for table, n in coverage.items():
                ledger.add('validation_reads', table, n)
        self._validation_seq += 1
        rec.update(
            status='TRUSTED' if ok else 'FAILED',
            validated_state=state,
            validated_seq=self._validation_seq,
            coverage=dict(coverage),
            witnesses=list(witnesses),
            counts=dict(counts),
        )
        return self.status(key)

    def validate_all(self, db, ledger=None):
        return [self.validate(key, db, ledger) for key in list(self._constraints)]

    # ---------- status：交付目录资格记录 ----------

    def status(self, key=None):
        if key is None:
            return [self.status(k) for k in self._constraints]
        rec = self._constraints[tuple(key)]
        return dict(rec)

    def identities(self):
        return [list(k) for k in self._constraints]

    def is_trusted(self, kind, table, column, parent=None, parent_column=None,
                   state_version=None):
        if kind == 'FOREIGN_KEY':
            key = (kind, table, column, parent, parent_column)
        else:
            key = (kind, table, column)
        rec = self._constraints.get(key)
        if rec is None or rec['status'] != 'TRUSTED':
            return False
        if state_version is not None and rec['validated_state'] != state_version:
            return False
        return True

    def join_evidence(self, kind, left_table, left_key, right_table, right_key, state_version):
        """返回某个等值 join 当前可用的目录证据及缺失前提。

        INNER 消除需要三件独立证据同时可信且处于同一源状态：
          1. referential_existence  FK：非 NULL 子键必有父（孤儿会被 INNER 删掉）；
          2. null_handling          NOT_NULL：子键无 NULL（NULL 行也会被 INNER 删掉）；
          3. uniqueness_parent_key  UNIQUE：父键唯一（父键重复会放大子行）。
        LEFT 消除只需要 at_most_one_match，即父键 UNIQUE；无匹配/NULL 本就保留子行。
        """
        unique = self._trust_record(('UNIQUE', right_table, right_key), state_version)
        pieces = dict(uniqueness_parent_key=unique)
        missing = [] if unique else ['UNIQUE(%s.%s)' % (right_table, right_key)]
        if kind == 'inner':
            fk = self._trust_record(
                ('FOREIGN_KEY', left_table, left_key, right_table, right_key), state_version)
            nn = self._trust_record(('NOT_NULL', left_table, left_key), state_version)
            pieces.update(referential_existence=fk, null_handling=nn)
            if not fk:
                missing.append('FOREIGN_KEY(%s.%s -> %s.%s)'
                               % (left_table, left_key, right_table, right_key))
            if not nn:
                missing.append('NOT_NULL(%s.%s)' % (left_table, left_key))
        return dict(complete=not missing, pieces=pieces, missing=missing)

    def _trust_record(self, key, state_version):
        rec = self._constraints.get(key)
        if rec is None or rec['status'] != 'TRUSTED':
            return None
        if rec['validated_state'] != state_version:
            return None
        return dict(identity=list(key), status='TRUSTED',
                    validated_state=rec['validated_state'],
                    validated_seq=rec['validated_seq'],
                    coverage=dict(rec['coverage']), counts=dict(rec['counts']))

    # ---------- 降级：unchecked 导入在数据可查询前调用 ----------

    def extend_trusted(self, staging_sizes, new_state, ledger=None):
        """受控批次成功后延展可信资格。

        批次提交前 enforce_trusted 已对暂存全表重跑过每条 TRUSTED 约束，
        因此资格可延展到新版本；覆盖范围更新为提交后的全表行数，
        维护读取成本已在 enforce_trusted 中如实记账。
        """
        extended = []
        for rec in self._constraints.values():
            if rec['status'] != 'TRUSTED':
                continue
            coverage = {rec['table']: staging_sizes[rec['table']]}
            if rec['kind'] == 'FOREIGN_KEY':
                coverage[rec['parent']] = staging_sizes[rec['parent']]
            rec['validated_state'] = new_state
            rec['coverage'] = coverage
            extended.append(list(rec['identity']))
        return extended

    def invalidate_for(self, table):
        """把所有覆盖该表的可信资格原子降回 DECLARED，返回被降级的身份列表。"""
        downgraded = []
        for rec in self._constraints.values():
            if rec['status'] != 'TRUSTED':
                continue
            touched = rec['table'] == table or rec.get('parent') == table
            if touched:
                rec.update(status='DECLARED', validated_state=None, validated_seq=None,
                           coverage=None, witnesses=[], counts=None)
                downgraded.append(list(rec['identity']))
        return downgraded

    # ---------- 受控写入时的在线维护检查（不改变资格状态） ----------

    def enforce_trusted(self, staging, db, ledger=None):
        """对暂存关系重跑所有 TRUSTED 约束；冲突抛 ConstraintViolation。"""
        for rec in self._constraints.values():
            if rec['status'] != 'TRUSTED':
                continue
            if rec['kind'] == 'NOT_NULL':
                rows = staging[rec['table']]
                if ledger is not None:
                    ledger.add('maintenance_reads', rec['table'], len(rows))
                nulls, witnesses = check_not_null(rows, rec['column'])
                if nulls:
                    raise ConstraintViolation(dict(rec), witnesses,
                                               dict(null_values=nulls))
            elif rec['kind'] == 'UNIQUE':
                rows = staging[rec['table']]
                if ledger is not None:
                    ledger.add('maintenance_reads', rec['table'], len(rows))
                dupes, witnesses = check_unique(rows, rec['column'])
                if dupes:
                    raise ConstraintViolation(dict(rec), witnesses,
                                               dict(duplicate_values=dupes))
            else:
                child_rows = staging[rec['table']]
                parent_rows = staging[rec['parent']]
                if ledger is not None:
                    ledger.add('maintenance_reads', rec['table'], len(child_rows))
                    ledger.add('maintenance_reads', rec['parent'], len(parent_rows))
                missing, _nulls, witnesses = check_fk(
                    child_rows, rec['column'], parent_rows, rec['parent_column'])
                if missing:
                    raise ConstraintViolation(dict(rec), witnesses,
                                               dict(missing_references=missing))
