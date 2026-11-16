"""可重复运行的验证入口：独立逐版本解释器核对历史快照范围查询。

Oracle 完全不使用引擎/索引代码，只消费引擎同样接收过的*操作事件流*
（单项 put/delete 与成功的 apply 批，各自记录 commit 边界），逐版本重放
出每个快照上的逻辑行表，再按 (category, row_id UTF-8 字节序) 过滤投影。

覆盖两类核对：
  1. 确定性场景：分类迁移、仅覆盖值修改、删除重插、同分类多行、
     NULL 与缺行、UTF-8 字节序、apply 失败原子性、空批；
  2. 固定种子的随机差分：双模式（覆盖 / 按版本身份回表）引擎在多个已提交
     快照、随机范围与投影上逐结果比对 oracle，并核对审计计数自洽。
运行：python3 verify.py [seed]
"""

import json
import random
import sys
from engine import Engine

CATEGORY_MIN, CATEGORY_MAX = -1000, 1000
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1
FAIL = []          # (名称, 说明)
CHECKS = [0]


def check(name, condition, detail=''):
    CHECKS[0] += 1
    if not condition:
        FAIL.append((name, detail))


class IndependentOracle:
    """逐版本解释器：维护与引擎相同的提交事件流，独立解释快照状态。"""

    def __init__(self):
        # 每个 row_id 的版本链：[(commit, deleted, category, amount), ...]
        self.chain = {}
        self.commit = 0

    def single(self, op, rid, category=None, amount=None):
        self.commit += 1
        self._append(rid, self.commit, op == 'delete', category, amount)

    def batch(self, operations):
        # 与 Engine.apply 相同的私有折叠规则，独立实现一遍。
        if not operations:
            return
        self.commit += 1
        live = set(self.chain)
        final = {}
        for item in operations:
            rid = item['row_id']
            if item['op'] == 'put':
                final[rid] = (item['category'], item['amount'])
                live.add(rid)
            else:
                final[rid] = None
                live.discard(rid)
        for rid, payload in final.items():
            existed = rid in self.chain
            if payload is None:
                if existed:
                    self._append(rid, self.commit, True, None, None)
            else:
                self._append(rid, self.commit, False, payload[0], payload[1])

    def _append(self, rid, commit, deleted, category, amount):
        self.chain.setdefault(rid, []).append(
            (commit, deleted, category, amount))

    def state_at(self, snapshot):
        rows = {}
        for rid, versions in self.chain.items():
            visible = [v for v in versions if v[0] <= snapshot]
            if not visible:
                continue
            commit, deleted, category, amount = visible[-1]
            if not deleted:
                rows[rid] = (category, amount)
        return rows

    def query(self, snapshot, lo, hi, projection):
        rows = self.state_at(snapshot)
        ordered = sorted(rows.items(),
                         key=lambda kv: (kv[1][0], kv[0].encode('utf-8')))
        out = []
        for rid, (category, amount) in ordered:
            if lo <= category < hi:
                record = dict(row_id=rid, category=category, amount=amount)
                out.append([record[col] for col in projection])
        return out


class Harness:
    """把同一事件流同时喂给双模式引擎与独立 oracle，并管理快照。"""

    def __init__(self):
        self.cover = Engine(covering=True)
        self.fetch = Engine(covering=False)
        self.oracle = IndependentOracle()
        self.snapshots = [0]
        for engine in (self.cover, self.fetch):
            engine.snapshot()

    def put(self, rid, category, amount):
        self._put_all(rid, category, amount)
        self._snapshot()

    def delete(self, rid):
        self._delete_all(rid)
        self._snapshot()

    def _put_all(self, rid, category, amount):
        self.cover.put(rid, category, amount)
        self.fetch.put(rid, category, amount)
        self.oracle.single('put', rid, category, amount)

    def _delete_all(self, rid):
        self.cover.delete(rid)
        self.fetch.delete(rid)
        self.oracle.single('delete', rid)

    def apply_ok(self, operations):
        c1 = self.cover.apply(operations)
        c2 = self.fetch.apply(operations)
        self.oracle.batch(operations)
        check('apply commit 双引擎一致', c1 == c2 == self.oracle.commit,
              f'{c1} vs {c2} vs {self.oracle.commit}')
        self._snapshot()

    def apply_expect_fail(self, operations, label):
        before = (self.cover.debug_state(), self.fetch.debug_state(),
                  self.oracle.commit, list(self.snapshots))
        for engine, name in ((self.cover, 'cover'), (self.fetch, 'fetch')):
            try:
                engine.apply(operations)
                check(label + f'({name}) 必须抛错', False)
            except ValueError:
                check(label + f'({name}) 抛 ValueError', True)
            check(label + f'({name}) 状态指纹不变',
                  engine.debug_state() == before[0 if name == 'cover' else 1])
        check(label + ' oracle commit 不变', self.oracle.commit == before[2])
        check(label + ' 已取得快照集合不变',
              list(self.snapshots) == before[3])

    def _snapshot(self):
        s1, s2 = self.cover.snapshot(), self.fetch.snapshot()
        check('双引擎当前 commit 一致', s1 == s2 == self.oracle.commit)
        self.snapshots.append(s1)

    def compare(self, lo, hi, projection, label):
        for engine, mode in ((self.cover, 'cover'), (self.fetch, 'fetch')):
            for snap in self.snapshots:
                got = engine.query(snap, lo, hi, projection)
                want = self.oracle.query(snap, lo, hi, projection)
                check(f'{label}[{mode}] snap={snap} {lo}..{hi} {projection}',
                      got == want, f'got={got} want={want}')
        self._check_audit(lo, hi)

    def _check_audit(self, lo, hi):
        # 审计自洽：覆盖模式零回表；回表模式每个命中项恰好一次按身份堆读取；
        # 两模式返回行数一致，且检查项数不小于返回数。
        ca, fa = self.cover.audit, self.fetch.audit
        check('覆盖模式不回表', ca['heap_versions_read'] == 0
              and ca['covering_reads'] == ca['index_entries_returned'])
        check('回表模式按版本身份每命中读一次',
              fa['heap_versions_read'] == fa['index_entries_returned']
              and fa['covering_reads'] == 0)
        check('审计计数自洽',
              ca['index_entries_examined'] >= ca['index_entries_returned']
              and fa['index_entries_examined'] == ca['index_entries_examined'])


def scenario_named():
    h = Harness()

    # 1) 分类迁移：旧快照旧类别不丢行，新快照新类别可见。
    h.put('a', 10, 100)
    h.put('b', 10, 50)
    h.put('a', 20, 100)                 # 仅改分类，amount 不变
    h.compare(10, 11, ['row_id', 'category', 'amount'], '迁移-旧类别')
    h.compare(20, 21, ['row_id', 'amount'], '迁移-新类别')

    # 2) 仅覆盖列修改（同分类改金额，含 NULL）。
    h.put('b', 10, None)
    h.put('b', 10, 51)
    h.compare(10, 11, ['row_id', 'category', 'amount'], '覆盖值修改')

    # 3) 删除后重插：旧身份版本不被覆盖，旧快照仍见旧值。
    h.delete('a')
    h.put('a', 30, 300)
    h.compare(20, 21, ['row_id', 'category', 'amount'], '删除前身份')
    h.compare(30, 31, ['row_id', 'category', 'amount'], '重插新身份')
    check('重插产生新版本身份',
          h.cover.heap.current('a')['version_id'] !=
          h.cover.heap.versions[0]['version_id'])

    # 4) 同分类多行 + UTF-8 字节序（含多字节与 ASCII 前缀情形）。
    for rid, cat in (('z', 7), ('é', 7), ('A', 7), ('aa', 7), ('中', 7)):
        h.put(rid, cat, 1)
    h.compare(7, 8, ['row_id'], 'UTF-8 字节序')
    want_order = sorted(['z', 'é', 'A', 'aa', '中'],
                        key=lambda s: s.encode('utf-8'))
    got_order = [r[0] for r in h.cover.query(h.snapshots[-1], 7, 8, ['row_id'])]
    check('UTF-8 字节序精确', got_order == want_order, f'{got_order}')

    # 5) NULL 金额与缺行区分：投影含 amount 时 NULL 保留 None，删除行不出现。
    h.put('n', 9, None)
    h.delete('n')
    h.put('n', 9, 7)
    h.compare(9, 10, ['row_id', 'amount'], 'NULL/缺行')

    # 6) 快照合法性：未产生 / 晚于当前 commit / 类型错误均拒绝。
    current = h.cover.snapshot()
    for bad, label in ((current + 1, '未来快照'),
                       (999999, '从未产生的快照'),
                       ('3', '字符串快照'), (True, '布尔快照')):
        try:
            h.cover.query(bad, 0, 1, ['row_id'])
            check(f'非法快照被拒绝:{label}', False)
        except ValueError:
            check(f'非法快照被拒绝:{label}', True)
    # 固定旧快照后继续写，旧快照读取仍是整批旧值。
    fixed = h.snapshots[3]
    fixed_before = h.cover.query(fixed, -1000, 1001,
                                 ['row_id', 'category', 'amount'])
    h.put('w', 500, 9)
    check('固定快照不受后续写入影响',
          h.cover.query(fixed, -1000, 1001,
                        ['row_id', 'category', 'amount']) == fixed_before)

    # 7) apply 原子性的确定性场景。
    h.apply_ok([{'op': 'put', 'row_id': 'x', 'category': 11, 'amount': 1},
                {'op': 'put', 'row_id': 'x', 'category': 12, 'amount': 2},
                {'op': 'delete', 'row_id': 'x'},
                {'op': 'put', 'row_id': 'x', 'category': 13, 'amount': 3}])
    check('批内反复改动以最后状态为准',
          h.cover.query(h.snapshots[-1], 13, 14,
                        ['row_id', 'category', 'amount']) == [['x', 13, 3]])
    h.apply_expect_fail([{'op': 'put', 'row_id': 'y', 'category': 0, 'amount': 0},
                         {'op': 'delete', 'row_id': 'ghost'}],
                        '删除不存在身份')
    h.apply_expect_fail([{'op': 'put', 'row_id': 'y', 'category': 0, 'amount': 0},
                         {'op': 'frobnicate', 'row_id': 'y'}],
                        '未知操作')
    h.apply_expect_fail([{'op': 'put', 'row_id': 'y',
                          'category': 1001, 'amount': 0}], '分类越界')
    h.apply_expect_fail([{'op': 'put', 'row_id': 'y', 'category': 0,
                          'amount': INT64_MAX + 1}], '金额越界')
    h.apply_expect_fail('not-a-list', '批类型非法')
    h.apply_expect_fail([{'op': 'put', 'row_id': '',
                          'category': 0, 'amount': 0}], '空 row_id')
    # 新身份同批先插后删：合法且折叠为零净变化，但仍分配一个 commit。
    commit_before = h.cover.snapshot()
    h.apply_ok([{'op': 'put', 'row_id': 'tmp', 'category': 1, 'amount': 1},
                {'op': 'delete', 'row_id': 'tmp'}])
    check('先插后删折叠且仍分配一个 commit',
          h.cover.snapshot() == commit_before + 1
          and h.cover.heap.current('tmp') is None)
    # 空批不分配 commit。
    commit_before = h.cover.snapshot()
    check('空批返回当前快照且不分配 commit',
          h.cover.apply([]) == commit_before
          and h.cover.snapshot() == commit_before)
    # 超过 100 项拒绝。
    h.apply_expect_fail([{'op': 'put', 'row_id': f'r{i}', 'category': 0,
                          'amount': 0} for i in range(101)], '批超长')
    h.compare(-1000, 1001, ['row_id', 'category', 'amount'], '最终全量')


def scenario_random(seed):
    rng = random.Random(seed)
    h = Harness()
    rids = [f'id{i:03d}' for i in range(12)]
    projections = [['row_id'], ['row_id', 'category'],
                   ['category', 'amount'], ['row_id', 'category', 'amount']]

    for step in range(160):
        rid = rng.choice(rids)
        live = rid in h.oracle.state_at(h.oracle.commit)
        choice = rng.random()
        if choice < 0.55 or not live:
            h.put(rid, rng.randint(CATEGORY_MIN, CATEGORY_MAX),
                  rng.choice([None, rng.randint(INT64_MIN, INT64_MAX),
                              rng.randint(-10, 10), 0]))
        elif choice < 0.75:
            h.delete(rid)
        else:
            # 随机批量：混入同身份反复改动，约 1/6 概率构造非法批。
            ops = []
            for _ in range(rng.randint(1, 8)):
                r = rng.choice(rids)
                if rng.random() < 0.8:
                    ops.append({'op': 'put', 'row_id': r,
                                'category': rng.randint(-50, 50),
                                'amount': rng.choice(
                                    [None, rng.randint(-100, 100)])})
                else:
                    ops.append({'op': 'delete', 'row_id': r})
            if rng.random() < 1 / 6:
                ops.append({'op': 'put', 'row_id': 'bad',
                            'category': 9999, 'amount': 0})
                h.apply_expect_fail(ops, f'随机非法批 step={step}')
            else:
                try:
                    h.apply_ok(ops)
                except ValueError:
                    # 删除私有状态中不存在身份也属合法失败路径，状态必须不变。
                    h.apply_expect_fail(ops, f'随机删除缺失 step={step}')
        if step % 8 == 0:
            lo = rng.randint(-60, 40)
            h.compare(lo, lo + rng.randint(1, 40),
                      rng.choice(projections), f'随机差分 step={step}')
    h.compare(CATEGORY_MIN, CATEGORY_MAX + 1,
              ['row_id', 'category', 'amount'], '随机终态全量')


def scenario_scale(seed):
    """题面上限附近：最多 500 行身份 × 每行 20 次替换/删除 × 20 个快照。"""
    rng = random.Random(seed)
    h = Harness()
    rids = [f'row-{i:04d}' for i in range(500)]
    for rid in rids:                                   # 初始装载，不取快照
        h._put_all(rid, rng.randint(-100, 100), rng.randint(-10, 10))
    h._snapshot()                                      # 第 1 个已提交快照

    total_writes = 500 * 20
    snapshot_every = total_writes // 19                # 连同首个快照共 20 个
    writes = 0
    for rep in range(20):
        rng.shuffle(rids)
        for rid in rids:
            live = rid in h.oracle.state_at(h.oracle.commit)
            if live and rng.random() < 0.3:
                h._delete_all(rid)
            else:
                h._put_all(rid, rng.randint(-1000, 1000),
                           rng.choice([None, rng.randint(INT64_MIN, INT64_MAX)]))
            writes += 1
            if len(h.snapshots) < 20 and writes % snapshot_every == 0:
                h._snapshot()
                h.compare(rng.randint(-50, 30), rng.randint(40, 120),
                          ['row_id', 'category', 'amount'],
                          f'规模差分 writes={writes}')
    check('规模场景已提交快照数为 20', len(h.snapshots) == 20)
    h.compare(-1000, 1001, ['row_id', 'category', 'amount'], '规模终态全量')
    check('规模场景身份数上限', len(h.oracle.chain) <= 500)
    check('规模场景每身份版本数上限',
          all(len(v) <= 21 for v in h.oracle.chain.values()))
    return h


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 410
    scenario_named()
    for s in (seed, seed + 1, seed + 2):
        scenario_random(s)
    h = scenario_scale(seed + 100)

    summary = dict(
        checks=CHECKS[0], failure_count=len(FAIL),
        final_heap_versions=h.cover.heap.history_length(),
        final_index_entries=h.cover.index.entry_count(),
        final_commit=h.cover.snapshot(),
        taken_snapshots=len(h.cover._taken_snapshots),
    )
    if FAIL:
        print(json.dumps(dict(ok=False, failures=FAIL[:20], **summary),
                         ensure_ascii=False, indent=2))
        sys.exit(1)
    print(json.dumps(dict(ok=True, **summary), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
