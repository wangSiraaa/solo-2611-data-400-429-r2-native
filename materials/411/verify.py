"""可复跑总验收：结构化用例 + 随机压力调度（≤1000行/4活跃/200操作）+ 线程交错。

运行：python3 verify.py [随机种子]
每条随机调度都经过 harness 的串行顺序枚举与无半批核对；随机用固定种子，
结果可复现。任何断言失败以非零状态退出。
"""

import json
import random
import sys
import threading
from pathlib import Path

from storage import Store
from transaction import (Transaction, ACTIVE, COMMITTED, CONFLICT, ABORTED,
                         INVALID_SAVEPOINT)
from harness import (run_file, run_case, enumerate_serial_orders,
                     check_no_half_batch, check_terminal_idempotence, Runner,
                     MAX_ACTIVE, MAX_OPS)

HERE = Path(__file__).parent
TEAM_LOW, TEAM_HIGH = -100, 100


def section(title):
    print(f'\n########## {title} ##########')


# ----------------------------------------------------------- 1) 结构化用例
def structured_suites():
    section('结构化调度（schedules.json + savepoint-cases.json）')
    all_reports = []
    for fname in ('schedules.json', 'savepoint-cases.json'):
        reports = run_file(HERE / fname)
        all_reports += reports
        for r in reports:
            print(f"  [{r['name']}] states={r['states']} "
                  f"合法串行顺序数={len(r['valid_serial_orders'])} "
                  f"操作数={r['ops_used']}")
            assert r['valid_serial_orders']
    return all_reports


# ----------------------------------------------------------- 2) 随机压力
def random_case(rng, idx, n_rows, n_tx, n_ops):
    rows = [dict(row_id=f'r{k:04d}',
                 team=rng.randint(TEAM_LOW, TEAM_HIGH),
                 on_call=rng.random() < 0.5) for k in range(n_rows)]
    ops = []
    alive = []
    sp_names = {}          # tx label -> 活跃保存点名集合
    used_ops = 0
    while used_ops < n_ops:
        # 优先保证所有事务 begin
        not_begun = [f'T{k}' for k in range(n_tx)
                     if f'T{k}' not in {op[1] for op in ops if op[0] == 'begin'}]
        if not_begun and (len(alive) < MAX_ACTIVE or rng.random() < 0.7):
            label = not_begun[0]
            ops.append(['begin', label])
            alive.append(label)
            sp_names[label] = set()
            used_ops += 1
            continue
        if not alive:
            break
        label = rng.choice(alive)
        kind = rng.choices(
            ['scan', 'get', 'put', 'delete', 'savepoint', 'rollback_to',
             'release', 'commit', 'abort'],
            weights=[26, 10, 22, 6, 8, 8, 4, 8, 8])[0]
        if kind == 'scan':
            center = rng.randint(TEAM_LOW, TEAM_HIGH)
            half = rng.randint(0, 5)
            low = max(TEAM_LOW, center - half)
            high = min(TEAM_HIGH, center + half)
            on_call = rng.choice([None, True, False])
            ops.append(['scan', label, low, high, on_call])
        elif kind == 'get':
            key = f'r{rng.randrange(max(1, n_rows + 4)):04d}'
            ops.append(['get', label, key])
        elif kind == 'put':
            key = f'r{rng.randrange(max(1, n_rows + 4)):04d}'
            ops.append(['put', label,
                        dict(row_id=key,
                             team=rng.randint(TEAM_LOW, TEAM_HIGH),
                             on_call=rng.random() < 0.5)])
        elif kind == 'delete':
            key = f'r{rng.randrange(max(1, n_rows)):04d}'
            ops.append(['delete', label, key])
        elif kind == 'savepoint':
            name = f'sp{rng.randrange(8)}'
            if name in sp_names[label] or len(sp_names[label]) >= 4:
                continue
            sp_names[label].add(name)
            ops.append(['savepoint', label, name])
        elif kind == 'rollback_to':
            if not sp_names[label]:
                continue
            name = rng.choice(sorted(sp_names[label]))
            # 回滚移除其后保存点
            ops.append(['rollback_to', label, name])
        elif kind == 'release':
            if not sp_names[label]:
                continue
            name = rng.choice(sorted(sp_names[label]))
            ops.append(['release', label, name])
            sp_names[label] = set()  # 简化：release 后清空模拟名集合
        elif kind == 'commit':
            ops.append(['commit', label])
            alive.remove(label)
            sp_names.pop(label, None)
        else:
            ops.append(['abort', label])
            alive.remove(label)
            sp_names.pop(label, None)
        used_ops += 1
    # 收尾：所有仍活跃事务必须明确结束（只读事务也要明确结束）
    for label in list(alive):
        ops.append([rng.choice(['commit', 'abort']), label])
        used_ops += 1
    assert len(ops) <= MAX_OPS
    return {'name': f'random_{idx}', 'seed': rows, 'ops': ops}


def stress_suite(seed):
    section(f'随机压力调度（seed={seed}）')
    rng = random.Random(seed)
    summaries = []
    for i in range(40):
        n_rows = rng.choice([0, 1, 5, 50, 200, 1000])
        n_tx = rng.randint(1, MAX_ACTIVE)
        n_ops = rng.randint(20, MAX_OPS - n_tx)
        case = random_case(rng, i, n_rows, n_tx, n_ops)
        report = run_case(case)
        summaries.append((report['name'], n_rows, n_tx, report['ops_used'],
                          report['states'], len(report['valid_serial_orders'])))
    for name, n_rows, n_tx, used, states, n_orders in summaries:
        print(f'  [{name}] rows={n_rows} tx={n_tx} ops={used} '
              f'states={states} 合法串行顺序数={n_orders}')
        assert n_orders >= 1
    return summaries


# ----------------------------------------------------------- 3) 线程交错
def threaded_interleave():
    section('真实线程交错：读路径无全局锁，commit 串行发布')
    store = Store([dict(row_id=f'k{i}', team=1, on_call=True)
                   for i in range(20)])
    errors = []

    def worker(label, keys, team):
        try:
            tx = Transaction(store, label=label)
            observed = tx.scan(1, 1, True)         # 两事务先都完成扫描
            barrier.wait()
            if len(observed) >= 2:
                for k in keys:
                    tx.put(dict(row_id=k, team=team, on_call=False))
            tx.commit()
        except Exception as exc:  # pragma: no cover - 测试守卫
            errors.append(exc)

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=worker, args=('W1', ['k0', 'k1'], 1))
    t2 = threading.Thread(target=worker, args=('W2', ['k2', 'k3'], 1))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, errors
    states = {}
    # 两个事务写不同键但读谓词交叠：恰一个成功（write-skew 防护），无半批
    head = store.head()
    committed_false = [k for k, r in head.items() if not r['on_call']]
    print(f'  最终离岗键={sorted(committed_false)}（应只有其中一个事务的2个键）')
    assert len(committed_false) == 2, committed_false
    # 历史逐 revision 仍可重放
    runner = Runner([dict(row_id=f'k{i}', team=1, on_call=True)
                     for i in range(20)], name='thread')
    runner.txs = {}  # 线程用例只做存储层原子性核对
    table = {f'k{i}': dict(row_id=f'k{i}', team=1, on_call=True)
             for i in range(20)}
    for rev in sorted(store.commits):
        meta = store.commits[rev]
        for key, (_old, new) in meta['delta'].items():
            if new is None:
                table.pop(key, None)
            else:
                table[key] = dict(new)
        assert table == store.history[rev]
    print('  每个 revision 原子可重放，无半批；读阶段未被互斥阻塞。')


# ----------------------------------------------------------- 4) 规模上限
def limits_check():
    section('规模上限：1000 行 / 4 活跃事务 / 200 操作')
    seed = [dict(row_id=f'r{k}', team=k % 201 - 100, on_call=bool(k % 2))
            for k in range(1000)]
    case = {'name': 'limits', 'seed': seed, 'ops': []}
    for k in range(4):
        case['ops'].append(['begin', f'T{k}'])
    for n in range((200 - 4 - 4) // 2):
        label = f'T{n % 4}'
        case['ops'].append(['scan', label, -100, 100,
                            None if n % 3 else True])
        case['ops'].append(['get', label, f'r{n % 1000}'])
    for k in range(4):
        case['ops'].append(['commit', f'T{k}'])
    assert len(case['ops']) <= MAX_OPS, len(case['ops'])
    report = run_case(case)
    print(f"  ops={report['ops_used']} revisions={report['revisions']} "
          f"合法串行顺序数={len(report['valid_serial_orders'])}")


if __name__ == '__main__':
    seed_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 20261116
    structured_suites()
    stress_suite(seed_arg)
    threaded_interleave()
    limits_check()
    section('全部验收通过')
    print(json.dumps({'status': 'PASS', 'seed': seed_arg},
                     ensure_ascii=False))
