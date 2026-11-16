"""独立模型核对与随机化验证（固定种子，可复现）。

独立模型：把操作按 seq 顺序逐条作用到普通 dict——
  put(key,value) 直接赋值；erase(lo,hi) 删除 dict 中落在 [lo,hi) 的键。
该模型与存储实现完全独立，是所有 get/scan 结果的对照基准。
"""
import json
import random
import tempfile
from pathlib import Path

from run_store import Store, KEY_MIN, KEY_MAX
from reader import get, scan, sweep_work
from compactor import compact
from inspect import inspect

P = lambda key, value, seq: {'kind': 'put', 'key': key, 'value': value, 'seq': seq}
D = lambda lo, hi, seq: {'kind': 'erase', 'lo': lo, 'hi': hi, 'seq': seq}

FAILURES = []


def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print('  [%s] %s %s' % (status, name, (':: ' + detail) if detail and not cond else ''))
    if not cond:
        FAILURES.append(name)


class DictOracle:
    """按 seq 顺序逐条作用到普通字典的参考模型。"""

    def __init__(self, records):
        self.state = {}
        for rec in sorted(records, key=lambda r: r['seq']):
            if rec['kind'] == 'put':
                self.state[rec['key']] = rec['value']
            else:
                # 只遍历当前存活的有限键，绝不枚举区间内所有整数
                for k in [k for k in self.state if rec['lo'] <= k < rec['hi']]:
                    del self.state[k]

    def get(self, key):
        return self.state.get(key)

    def scan(self, lo, hi):
        return [[k, self.state[k]] for k in sorted(self.state) if lo <= k < hi]

    def snapshot(self):
        return dict(self.state)


def full_scan(store):
    return scan(store, KEY_MIN, KEY_MAX + 1)


def assert_matches(store, oracle, label):
    expect = sorted(oracle.state.items())
    actual = [(k, v) for k, v in full_scan(store)]
    check('%s: full scan == dict oracle' % label, actual == expect,
          'oracle=%r actual=%r' % (expect, actual))
    for key in rng_gets:
        check('%s: get(%d)' % (label, key), get(store, key) == oracle.get(key))
    for lo, hi in [(-15, 0), (0, 10), (-7, 7), (10, 20), (-100, 100)]:
        check('%s: scan(%d,%d)' % (label, lo, hi),
              scan(store, lo, hi) == oracle.scan(lo, hi))


# ---------- 手工场景 ----------
def handcrafted_cases():
    print('== 手工场景 ==')
    # 1. 同区间删后重插
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        hist = [P(1, 'a', 1), P(2, 'b', 2), D(0, 10, 3), P(1, 'c', 4)]
        s.add('r0', hist)
        o = DictOracle(hist)
        assert_matches(s, o, 'delete-then-reinsert')
        receipt = compact(s, ['r0'], 'all')
        check('full compact drops erase', receipt['erase_records'] == 0)
        assert_matches(s, o, 'after full compact')

    # 2. 多层交叠/嵌套（fixtures.overlap）
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        hist = json.loads((Path(__file__).parent / 'fixtures.json').read_text())['overlap']
        # 拆到三个 run，制造跨段
        s.add('r0', hist[0:2]); s.add('r1', hist[2:4]); s.add('r2', hist[4:])
        o = DictOracle(hist)
        assert_matches(s, o, 'nested overlaps cross-run')
        compact(s, ['r0', 'r1'], 'm0')
        assert_matches(s, o, 'nested overlaps after partial compact r0+r1')
        compact(s, ['m0', 'r2'], 'all')
        check('nested: full compact has no erases',
              not any(r['kind'] == 'erase' for r in s.read('all')))
        assert_matches(s, o, 'nested overlaps after full compact')

    # 3. 端点：lo 含、hi 不含；相接区间
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        hist = [P(10, 'lo-edge', 1), P(19, 'inside', 2), P(20, 'hi-edge', 3),
                D(10, 20, 4), D(20, 30, 5), P(20, 'back', 6), P(29, 'late', 7)]
        s.add('r0', hist[:4]); s.add('r1', hist[4:])
        o = DictOracle(hist)
        assert_matches(s, o, 'half-open endpoints + touching ranges')

    # 4. 旧 erase 不能误删新点；新点可在删除后重现
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        s.add('old', [D(0, 100, 2)])
        s.add('new', [P(50, 'fresh', 3), P(5, 'also', 4)])
        o = DictOracle([D(0, 100, 2), P(50, 'fresh', 3), P(5, 'also', 4)])
        assert_matches(s, o, 'older erase vs newer puts')

    # 5. 局部压缩的回收正/反例
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        s.add('old', [P(50, 'oldv', 1)])
        s.add('del', [D(0, 100, 5)])
        s.add('fresh', [P(70, 'newv', 6)])
        o = DictOracle([P(50, 'oldv', 1), D(0, 100, 5), P(70, 'newv', 6)])
        r = compact(s, ['del', 'fresh'], 'mf')
        check('erase kept while it shields old run point', r['erase_records'] == 1)
        check('fresh point survives', r['point_records'] == 1)
        assert_matches(s, o, 'partial compact keeps protection')
        r2 = compact(s, ['old', 'mf'], 'full')
        check('full compact reclaims erase', r2['erase_records'] == 0 and r2['reclaimed_erases'] == 1)
        check('full compact keeps only visible points', r2['point_records'] == 1)
        assert_matches(s, o, 'after full compact reclaim')

    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        s.add('old', [P(50, 'older-than-erase?', 1)])
        s.add('del', [D(0, 100, 5)])
        # 只压缩 delete：old 段存在 seq 更小的点 -> 必须保留 erase
        r = compact(s, ['del'], 'd1')
        check('erase retained when unselected older point exists', r['erase_records'] == 1)
        # 反向：erase 比所有未选点都旧 -> 无保护对象 -> 回收
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        # 逻辑先后由 seq 决定：erase(5) 先于 put(9)，add 顺序仍须满足 seq 单调
        s.add('del', [D(0, 100, 5)])
        s.add('later', [P(50, 'newer', 9)])
        r = compact(s, ['del'], 'd1')
        check('erase reclaimed when no unselected point is older',
              r['erase_records'] == 0 and r['reclaimed_erases'] == 1)
        check('reclaimed erase never hid newer point', get(s, 50) == 'newer')

    # 6. demo 关键断言：只压缩 delete/fresh 后仍须遮住 old 段的 10 号键
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        s.add('old', [P(10, 'old', 1), P(30, 'keep', 2)])
        s.add('delete', [D(0, 20, 3)])
        s.add('fresh', [P(12, 'new', 4)])
        compact(s, ['delete', 'fresh'], 'merged')
        check('key 10 stays hidden after partial compact', get(s, 10) is None)
        check('key 30 remains visible', get(s, 30) == 'keep')
        check('key 12 reinserted visible', get(s, 12) == 'new')

    # 7. 重复局部压缩不丢失保护，且撤下 run 文件消失
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        s.add('a', [P(1, 'x', 1)])
        s.add('b', [D(0, 10, 2)])
        s.add('c', [P(11, 'y', 3)])
        s.add('d', [D(10, 20, 4)])
        o = DictOracle([P(1, 'x', 1), D(0, 10, 2), P(11, 'y', 3), D(10, 20, 4)])
        compact(s, ['b', 'c'], 'bc')
        assert_matches(s, o, 'repeated partial 1')
        files = {p.stem for p in Path(d).glob('*.json')}
        check('retired run files removed', not ({'b', 'c'} & files))
        compact(s, ['bc', 'd'], 'bcd')
        assert_matches(s, o, 'repeated partial 2')
        compact(s, ['a', 'bcd'], 'done')
        assert_matches(s, o, 'repeated partial -> full')
        check('no erase after full compact',
              all(r['kind'] == 'put' for r in s.read('done')))


# ---------- 固定种子随机历史 ----------
def make_history(rng, n=60):
    hist = []
    seq = 0
    for _ in range(n):
        seq += 1
        kind = rng.choices(['put', 'erase', 'wide_erase'], weights=[6, 3, 1])[0]
        if kind == 'put':
            hist.append(P(rng.randint(-15, 15), 'v%d' % seq, seq))
        elif kind == 'erase':
            a = rng.randint(-20, 19)
            b = rng.randint(a + 1, 20)
            hist.append(D(a, b, seq))
        else:
            # 大跨度区间：边界仍是两个整数，物理记录只有一条
            lo = rng.choice([KEY_MIN, -10 ** 6, -1000])
            hi = rng.choice([KEY_MAX, 10 ** 6, 1000])
            if lo >= hi:
                lo, hi = KEY_MIN, KEY_MAX
            hist.append(D(lo, hi, seq))
    return hist


def segmentations(hist):
    """同一历史的多种合法切段（按 seq 顺序装入，切段位置不同）。"""
    n = len(hist)
    cuts = {
        'single': [n],
        'chunks_of_5': [5] * (n // 5) + ([n % 5] if n % 5 else []),
        'chunks_of_7': [7] * (n // 7) + ([n % 7] if n % 7 else []),
    }
    # 随机切点，run 数不超过 16
    rng_local = random.Random(99)
    k = rng_local.randint(3, 15)
    points = sorted(rng_local.sample(range(1, n), min(k, n - 1)))
    sizes = [points[0]] + [points[i] - points[i - 1] for i in range(1, len(points))] + [n - points[-1]]
    cuts['random_cuts'] = sizes
    segs = {}
    for name, sizes in cuts.items():
        runs, idx = [], 0
        for sz in sizes:
            runs.append(hist[idx:idx + sz])
            idx += sz
        assert idx == n and len(runs) <= 16
        segs[name] = runs
    return segs


def fuzz_history(hist, seed_tag):
    oracle = DictOracle(hist)
    for seg_name, runs in segmentations(hist).items():
        with tempfile.TemporaryDirectory() as d:
            s = Store(d)
            for i, recs in enumerate(runs):
                s.add('r%d' % i, recs)
            label = '%s/%s' % (seed_tag, seg_name)
            assert_matches(s, oracle, label + ' initial')

            # 随机顺序的局部压缩，每步都对照独立模型（种子完全确定，可复现）
            rng = random.Random(sum((i + 1) * ord(c) for i, c in enumerate(seed_tag + seg_name)))
            step = 0
            retired = set()
            if len(s.ids()) == 1:
                # 单段历史也要验证全量压缩：输出只含可见点、erase 全部回收
                only_id = s.ids()[0]
                retired.add(only_id)
                compact(s, [only_id], 'c0')
                step = 1
                assert_matches(s, oracle, label + ' full compact from single run')
            while len(s.ids()) > 1:
                k = rng.randint(1, min(3, len(s.ids())))
                subset = rng.sample(s.ids(), k)
                retired.update(subset)
                compact(s, subset, 'c%d' % step)
                step += 1
                assert_matches(s, oracle, label + ' compact#%d' % step)
            check(label + ': one run left', len(s.ids()) == 1)
            only = s.read(s.ids()[0])
            check(label + ': full compact stores zero erases',
                  not any(r['kind'] == 'erase' for r in only))
            check(label + ': full compact visible set equals oracle',
                  sorted((r['key'], r['value']) for r in only) ==
                  sorted(oracle.state.items()))

            # 压缩后再导入更晚的 put/erase，继续查询与压缩
            extra = [P(7, 'late', len(hist) + 1), D(-15, 8, len(hist) + 2),
                     P(7, 'rebirth', len(hist) + 3)]
            s.add('late', extra)
            oracle2 = DictOracle(hist + extra)
            assert_matches(s, oracle2, label + ' after late import')
            compact(s, [s.ids()[0], 'late'], 'again')
            assert_matches(s, oracle2, label + ' compact after late import')
            check(label + ': no erases after second full compact',
                  all(r['kind'] == 'put' for r in s.read(s.ids()[0])))

            # 重开目录：持久化后结果不变
            s2 = Store(d)
            assert_matches(s2, oracle2, label + ' reopened')
            info = inspect(s2)
            check(label + ': journal persisted across reopen',
                  len(info['compaction_journal']) == step + 1)
            files = {p.stem for p in Path(d).glob('*.json')}
            check(label + ': retired run files removed from disk',
                  retired.isdisjoint(files))
            check(label + ': every active run has a file',
                  set(s2.ids()) <= files)


def wide_span_not_expanded():
    print('== 大跨度 erase 不展开 ==')
    pts = [P(1, 'a', 1), P(2, 'b', 2), P(-3, 'c', 3)]
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        s_narrow = Store(d1)
        s_narrow.add('r', pts + [D(-5, 5, 10)])
        s_wide = Store(d2)
        s_wide.add('r', pts + [D(KEY_MIN, KEY_MAX, 10)])
        _, wn = sweep_work(s_narrow)
        _, ww = sweep_work(s_wide)
        check('sweep work independent of span', wn == ww, '%r vs %r' % (wn, ww))
        rec = s_wide.read('r')[-1]
        check('wide erase stored as one bounded record',
              rec == {'kind': 'erase', 'lo': KEY_MIN, 'hi': KEY_MAX, 'seq': 10})
        check('wide erase hides all three points', full_scan(s_wide) == [])
        check('narrow erase same visibility semantics', full_scan(s_narrow) == [])


def validation_cases():
    print('== 输入校验与持久化约束 ==')
    with tempfile.TemporaryDirectory() as d:
        s = Store(d)
        s.add('r0', [P(1, 'a', 5), D(0, 2, 6)])
        for bad, name in [
            ([P(1, 'x', 5)], 'duplicate seq'),
            ([P(1, 'x', 3)], 'older seq after import'),
            ([D(3, 3, 7)], 'empty erase range'),
            ([D(4, 2, 7)], 'inverted erase range'),
            ([P(1, '', 7)], 'empty value'),
        ]:
            try:
                s.add('rx', bad)
                check(name, False)
            except ValueError:
                check(name, True)
        try:
            s.add('r0', [P(2, 'y', 8)])
            check('run id collision rejected', False)
        except ValueError:
            check('run id collision rejected', True)
        # 压缩后旧 seq 依然不允许重新导入
        compact(s, ['r0'], 'out')
        try:
            s.add('rz', [P(9, 'z', 4)])
            check('pre-compaction seq rejected after compact', False)
        except ValueError:
            check('pre-compaction seq rejected after compact', True)
        s.add('rn', [P(9, 'z', 100)])
        check('seq larger than all history accepted after compact', get(s, 9) == 'z')
        # 16 run 上限
        d3 = tempfile.mkdtemp()
        s3 = Store(d3)
        seq = 0
        for i in range(16):
            seq += 1
            s3.add('r%02d' % i, [P(i, 'v', seq)])
        try:
            seq += 1
            s3.add('rx', [P(100, 'v', seq)])
            check('17th run rejected', False)
        except ValueError:
            check('17th run rejected', True)


if __name__ == '__main__':
    rng_gets = [-100, -16, -15, -1, 0, 1, 7, 10, 15, 16, 100]
    handcrafted_cases()
    wide_span_not_expanded()
    validation_cases()
    print('== 固定种子随机历史（多种切段 × 压缩顺序 × 重开）==')
    for seed in (405, 2024, 7):
        rng = random.Random(seed)
        fuzz_history(make_history(rng, n=60), 'seed%d' % seed)
    print()
    if FAILURES:
        print('FAILURES: %d' % len(FAILURES))
        for f in FAILURES:
            print(' -', f)
        raise SystemExit(1)
    print('ALL CHECKS PASSED')
