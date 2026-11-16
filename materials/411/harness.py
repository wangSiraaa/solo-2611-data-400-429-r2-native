"""受控调度执行器 + 串行顺序枚举核对器（可复跑调度，无硬编码结果）。

职责：
  1. 解释 JSON 中的操作序列（begin/get/scan/put/delete/savepoint/.../commit/
     abort），真实驱动 Transaction/Store，不预设任何调度结果；
  2. 用小历史枚举【成功事务的所有串行排列】，把每个事务实际返回的 get/scan
     对照“该排列中它之前的库状态 + 它自己依据的私有版本”重放，核对每次读取
     与最终表；
  3. 核对失败/回滚没有发布半批：每个 revision 的增量恰为一个事务的完整写集，
     冲突事务的写不出现在任何快照中；
  4. 约束：同时活跃事务 ≤ 4、操作总数 ≤ 200（保存点操作计入）。
"""

import itertools
import json
from pathlib import Path

from storage import Store
from transaction import (Transaction, ACTIVE, COMMITTED, CONFLICT, ABORTED,
                         INVALID_SAVEPOINT)
from validation import GetRead, ScanRead

MAX_ACTIVE = 4
MAX_OPS = 200
TERMINAL = {COMMITTED, CONFLICT, ABORTED}


# ------------------------------------------------------------- 调度执行
class Runner:
    def __init__(self, seed, name='run'):
        self.store = Store(seed)
        self.txs = {}
        self.name = name
        self.ops_count = 0
        self.trace = []

    def _count(self):
        self.ops_count += 1
        if self.ops_count > MAX_OPS:
            raise AssertionError(f'{self.name}: 超过 {MAX_OPS} 次操作上限')

    def _active_count(self):
        return sum(1 for t in self.txs.values() if t.state == ACTIVE)

    def run_op(self, op):
        verb = op[0]
        self._count()
        if verb == 'begin':
            label = op[1]
            if self._active_count() >= MAX_ACTIVE:
                raise AssertionError(f'{self.name}: 同时活跃事务超过 {MAX_ACTIVE}')
            tx = Transaction(self.store, label=label)
            self.txs[label] = tx
            self.trace.append([verb, label, tx.start])
            return tx.start
        tx = self.txs[op[1]]
        if verb == 'get':
            result = tx.get(op[2])
            self.trace.append([verb, op[1], op[2], result])
            return result
        if verb == 'scan':
            low, high = op[2], op[3]
            on_call = op[4] if len(op) > 4 else None
            result = tx.scan(low, high, on_call)
            self.trace.append([verb, op[1], low, high, on_call, result])
            return result
        if verb == 'put':
            result = tx.put(dict(op[2]))
            self.trace.append([verb, op[1], op[2]])
            return result
        if verb == 'delete':
            result = tx.delete(op[2])
            self.trace.append([verb, op[1], op[2]])
            return result
        if verb == 'savepoint':
            result = tx.savepoint(op[2])
            self._expect(op, result, 3)
            self.trace.append([verb, op[1], op[2], result])
            return result
        if verb == 'rollback_to':
            result = tx.rollback_to(op[2])
            self._expect(op, result, 3)
            self.trace.append([verb, op[1], op[2], result])
            return result
        if verb == 'release':
            result = tx.release(op[2])
            self._expect(op, result, 3)
            self.trace.append([verb, op[1], op[2], result])
            return result
        if verb == 'commit':
            result = tx.commit()
            self._expect(op, result, 2)
            self.trace.append([verb, op[1], result])
            return result
        if verb == 'abort':
            result = tx.abort()
            self._expect(op, result, 2)
            self.trace.append([verb, op[1], result])
            return result
        raise AssertionError(f'未知操作: {op}')

    @staticmethod
    def _expect(op, actual, idx):
        if len(op) > idx and op[idx] is not None:
            assert actual == op[idx], (
                f'期望 {op[:idx + 1]} 得到 {actual}')

    def run_all(self, ops):
        for op in ops:
            self.run_op(op)


# ------------------------------------------------------------- 串行顺序枚举
def _writes_visible_at(tx, wv_at_read):
    """串行重放中，某条读取能看见的本事务私有写：版本号不超过读取水位。"""
    out = {}
    for key, wv in tx._key_wv.items():
        if wv <= wv_at_read:
            out[key] = tx.private_versions[wv]
    return out


def _replay_get(base, tx, read):
    if read.source == 'private':
        value = tx.private_versions[read.wv]
        return None if value is None else dict(value)
    row = base.get(read.key)
    return None if row is None else dict(row)


def _replay_scan(base, tx, read):
    # 只叠加该读取发生时已经存在的本事务私有写（含私有墓碑）；读取之后的
    # put/delete 不能改变更早返回的结果。provenance 中的历史私有版本负责
    # 复现“回滚后已不在当前写集、但读取当时确实返回过”的值。
    effective = dict(base)
    for key, value in _writes_visible_at(tx, read.wv_at_read).items():
        if value is None:
            effective.pop(key, None)
        else:
            effective[key] = dict(value)
    for key, (source, wv) in read.provenance.items():
        if source == 'private' and wv <= read.wv_at_read:
            value = tx.private_versions[wv]
            if value is None:
                effective.pop(key, None)
            else:
                effective[key] = dict(value)
    matched = {}
    for key, row in effective.items():
        if read.low <= row['team'] <= read.high and \
                (read.on_call is None or row['on_call'] == read.on_call):
            matched[key] = dict(row)
    return matched


def _order_valid(seed, order):
    """按给定排列重放：每个事务的读取必须在它之前的库状态上可复现。"""
    table = {r['row_id']: dict(r) for r in seed}
    for tx in order:
        for read in tx.reads:
            if isinstance(read, GetRead):
                got = _replay_get(table, tx, read)
                if got != read.observed:
                    return False
            elif isinstance(read, ScanRead):
                got = _replay_scan(table, tx, read)
                if sorted(got) != read.observed or \
                        any(got[k] != read.matched[k] for k in read.observed):
                    return False
        # 该事务在排列中执行其写
        for key, value in tx.writes.items():
            if value is None:
                table.pop(key, None)
            else:
                table[key] = dict(value)
    return True


def enumerate_serial_orders(seed, txs):
    committed = [t for t in txs if t.state == COMMITTED]
    valid = []
    for perm in itertools.permutations(committed):
        if _order_valid(seed, perm):
            valid.append([t.label for t in perm])
    return valid


# ------------------------------------------------------------- 不变量核对
def check_no_half_batch(seed, runner):
    """每个 revision 只含一个事务的完整写集；冲突/回滚的写绝不出现。"""
    store = runner.store
    # 1) 逐 revision 重放历史快照，验证发布原子性
    table = {r['row_id']: dict(r) for r in seed}
    label_to_tx = {t.label: t for t in runner.txs.values()}
    for rev in sorted(store.commits):
        meta = store.commits[rev]
        tx = label_to_tx[meta['owner']]
        assert set(meta['delta']) == set(tx.writes), (
            f'revision {rev} 不是事务 {tx.label} 的完整写集（半批？）')
        for key, value in tx.writes.items():
            if value is None:
                table.pop(key, None)
            else:
                table[key] = dict(value)
        assert table == store.history[rev], f'revision {rev} 快照与重放不一致'
    # 2) 最终表 = seed 叠加所有且仅成功事务的写
    expected = {r['row_id']: dict(r) for r in seed}
    for tx in runner.txs.values():
        if tx.state == COMMITTED:
            for key, value in tx.writes.items():
                if value is None:
                    expected.pop(key, None)
                else:
                    expected[key] = dict(value)
    assert expected == store.head(), '最终表含未成功事务的写入（泄漏）'
    # 3) 冲突/中止事务必须带终态且未产生 revision
    for tx in runner.txs.values():
        if tx.state in (CONFLICT, ABORTED):
            assert all(store.commits[r]['owner'] != tx.label
                       for r in store.commits), f'{tx.label} 失败却发布了数据'
        if tx.state == CONFLICT:
            assert tx.conflict_evidence, f'{tx.label} CONFLICT 但没有冲突证据'
            for finding in tx.conflict_evidence:
                assert finding.get('read_basis') or finding.get('kind') == \
                    'write_write', '冲突证据缺少读取依据'
    # 4) 实际提交顺序本身必须是合法串行顺序
    commit_order = [label_to_tx[store.commits[r]['owner']]
                    for r in sorted(store.commits)]
    if commit_order:
        assert _order_valid(seed, commit_order), \
            '实际 commit 顺序不能串行化成功事务'


def check_terminal_idempotence(runner):
    """终态事务重复提交/重复查询返回第一次结果（抽样由调度中的重复操作覆盖，
    这里对每个终态事务再做一次幂等校验）。"""
    for tx in runner.txs.values():
        if tx.state in TERMINAL:
            again = tx.commit() if tx.state != ABORTED else tx.abort()
            assert again == tx.state
            frozen = tx.scan(-100, 100)
            assert tx.scan(-100, 100) == frozen  # 重复查询稳定


# ------------------------------------------------------------- 单条用例
def run_case(case):
    runner = Runner(case['seed'], case.get('name', 'run'))
    runner.run_all(case['ops'])
    txs = list(runner.txs.values())
    valid_orders = enumerate_serial_orders(case['seed'], txs)
    assert valid_orders, '成功事务不存在任何可串行化顺序'
    check_no_half_batch(case['seed'], runner)
    check_terminal_idempotence(runner)
    states = {label: tx.state for label, tx in runner.txs.items()}
    return {
        'name': case.get('name'),
        'states': states,
        'revisions': runner.store.revision,
        'final_table': runner.store.head(),
        'valid_serial_orders': valid_orders,
        'ops_used': runner.ops_count,
        'conflict_evidence': {
            tx.label: tx.conflict_evidence
            for tx in txs if tx.state == CONFLICT},
        'read_basis': {tx.label: tx.read_basis() for tx in txs},
    }


def run_file(path):
    data = json.loads(Path(path).read_text())
    reports = []
    for case in data['runs']:
        seed = case.get('seed', data.get('seed', data.get('initial')))
        report = run_case({'name': case['name'], 'seed': seed,
                           'ops': case['ops']})
        reports.append(report)
    return reports
