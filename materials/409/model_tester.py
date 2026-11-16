"""Model-based verification against the real page/WAL facilities.

An independent transaction-effect oracle (only committed ADDs contribute to a
key's final value; an aborted transaction contributes nothing regardless of how
many flushes happened) is compared byte-for-byte with the recovered page files.

Coverage:
  * fixed schedules: interleaved winners/losers on one page, repeated writes to
    the same key, multiple pages, negative deltas, post-recovery reuse;
  * randomized legal schedules (bounded to the stated family: <=8 pages,
    <=32 keys/page, <=4 active transactions, one open writer per key,
    <=100 business ADDs and <=20 injected aborts);
  * rejected requests: LOCKED, bool delta, int64 overflow -- each must leave
    pages, WAL and locks untouched;
  * every named recovery fault point, resumed repeatedly until READY, including
    a second interruption after partial recovery progress;
  * a hand-crafted page_lsn-beyond-WAL directory that must fail closed;
  * untouched pages and keys (p1/reserve=12345, 731-style values) surviving.
"""
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
ALL_FAULTS = (None, 'after-undo-log', 'after-abort', 'after-redo-write', 'before-ready')


def invoke(args):
    result = subprocess.run([sys.executable, '-B', *map(str, args)],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout, result.stderr


def make_seed(directory, layout):
    pages = directory / 'pages'
    pages.mkdir(parents=True)
    for page_id, spec in layout.items():
        (pages / (page_id + '.json')).write_text(json.dumps(
            {'page_id': page_id, 'page_lsn': 0, 'rows': spec['rows']},
            sort_keys=True))
    (directory / 'wal.jsonl').write_text('')


def read_pages(directory):
    result = {}
    for path in sorted((directory / 'pages').glob('*.json')):
        result[path.stem] = json.loads(path.read_text())
    return result


def read_wal(directory):
    return [json.loads(line) for line in
            (directory / 'wal.jsonl').read_text().splitlines() if line]


def expected_final(layout, committed_effects):
    expected = {page_id: dict(spec['rows']) for page_id, spec in layout.items()}
    for (page_id, key), delta in committed_effects.items():
        expected[page_id][key] += delta
        assert INT64_MIN <= expected[page_id][key] <= INT64_MAX
    return expected


def recover_until_ready(directory, faults, label):
    """Drive recovery through the given fault sequence, then once cleanly."""
    observed_exits = []
    for fault in faults:
        if fault is None:
            code, stdout, stderr = invoke(['main.py', 'recover', directory])
        else:
            code, stdout, stderr = invoke(['main.py', 'recover', directory,
                                           '--fault', fault])
        observed_exits.append(code)
        if code == 0:
            return json.loads(stdout), observed_exits
        assert code in (75, 76, 77, 78), (label, fault, code, stdout, stderr)
    code, stdout, stderr = invoke(['main.py', 'recover', directory])
    assert code == 0, (label, code, stdout, stderr)
    observed_exits.append(code)
    return json.loads(stdout), observed_exits


def check_final(directory, layout, committed_effects, label):
    on_disk = read_pages(directory)
    expected = expected_final(layout, committed_effects)
    actual_rows = {page_id: page['rows'] for page_id, page in on_disk.items()}
    assert actual_rows == expected, (label, actual_rows, expected)
    # Every key still exists, no key was added or deleted.
    assert sorted(on_disk) == sorted(layout)
    for page_id, spec in layout.items():
        assert sorted(on_disk[page_id]['rows']) == sorted(spec['rows'])
        assert all(INT64_MIN <= v <= INT64_MAX
                   for v in on_disk[page_id]['rows'].values())
    # WAL must remain an ordered, gap-free prefix.
    wal = read_wal(directory)
    assert [record['lsn'] for record in wal] == list(range(1, len(wal) + 1))
    # Each ADD is compensated at most once; loser ADDs are all compensated.
    undos = [record for record in wal if record['kind'] == 'UNDO']
    undo_of = [record['undo_of'] for record in undos]
    assert sorted(undo_of) == sorted(set(undo_of)), label
    return wal, on_disk


# ---------------------------------------------------------------------------
# Fixed schedules with an explicit independent oracle
# ---------------------------------------------------------------------------

def fixed_schedules():
    layout = {
        'p0': {'rows': {'alpha': 10, 'beta': 20, 'gamma': -5, 'untouched': 731}},
        'p1': {'rows': {'reserve': 12345}},
    }
    # name, schedule, committed effects
    return layout, [
        ('interleaved-multi-key', [
            ('begin', 'T1'), ('add', 'T1', 'p0', 'alpha', 91),
            ('begin', 'T2'), ('add', 'T2', 'p0', 'beta', 2),
            ('add', 'T1', 'p0', 'alpha', -3),
            ('flush', None, 'p0', None, None),
            ('commit', 'T2'), ('crash',),
        ], {('p0', 'beta'): 2}),
        ('same-key-repeated-winner', [
            ('begin', 'T1'), ('add', 'T1', 'p0', 'alpha', 1),
            ('add', 'T1', 'p0', 'alpha', 2), ('add', 'T1', 'p0', 'alpha', 3),
            ('add', 'T1', 'p0', 'alpha', -6),
            ('commit', 'T1'), ('crash',),
        ], {('p0', 'alpha'): 0}),
        ('multi-page-losers', [
            ('begin', 'T1'), ('add', 'T1', 'p0', 'beta', 100),
            ('add', 'T1', 'p1', 'reserve', -100),
            ('begin', 'T2'), ('add', 'T2', 'p0', 'gamma', 7),
            ('flush', None, 'p0', None, None),
            ('commit', 'T2'), ('abort', 'T1'), ('crash',),
        ], {('p0', 'gamma'): 7}),
        ('negative-deltas-committed', [
            ('begin', 'T1'), ('add', 'T1', 'p0', 'alpha', -4),
            ('add', 'T1', 'p0', 'beta', -20),
            ('commit', 'T1'), ('flush', None, 'p0', None, None),
            ('crash',),
        ], {('p0', 'alpha'): -4, ('p0', 'beta'): -20}),
        ('abort-then-reuse-same-key', [
            ('begin', 'T1'), ('add', 'T1', 'p0', 'alpha', 500),
            ('flush', None, 'p0', None, None), ('abort', 'T1'),
            ('begin', 'T2'), ('add', 'T2', 'p0', 'alpha', 17),
            ('commit', 'T2'), ('crash',),
        ], {('p0', 'alpha'): 17}),
        ('all-four-active-interleaved', [
            ('begin', 'T1'), ('add', 'T1', 'p0', 'alpha', 1),
            ('begin', 'T2'), ('add', 'T2', 'p0', 'beta', 2),
            ('begin', 'T3'), ('add', 'T3', 'p0', 'gamma', 3),
            ('begin', 'T4'), ('add', 'T4', 'p1', 'reserve', 4),
            ('commit', 'T1'), ('commit', 'T3'), ('commit', 'T4'),
            ('abort', 'T2'),
            ('flush', None, 'p0', None, None), ('crash',),
        ], {('p0', 'alpha'): 1, ('p0', 'gamma'): 3, ('p1', 'reserve'): 4}),
        ('four-way-winner-loser-flush', [
            ('begin', 'T1'), ('add', 'T1', 'p0', 'alpha', 1),
            ('begin', 'T2'), ('add', 'T2', 'p0', 'beta', 2),
            ('begin', 'T3'), ('add', 'T3', 'p0', 'gamma', 3),
            ('begin', 'T4'), ('add', 'T4', 'p1', 'reserve', 4),
            # Only T1 commits; T2/T3/T4 are losers whose pages get stolen.
            ('commit', 'T1'),
            ('flush', None, 'p0', None, None),
            ('flush', None, 'p1', None, None), ('crash',),
        ], {('p0', 'alpha'): 1}),
    ]


def to_event(step):
    op = step[0]
    if op in ('begin', 'commit', 'abort'):
        return {'op': op, 'tx': step[1]}
    if op == 'add':
        return {'op': 'add', 'tx': step[1], 'page': step[2],
                'key': step[3], 'delta': step[4]}
    if op == 'flush':
        return {'op': 'flush', 'page': step[2]}
    return {'op': op}


def run_fixed(report):
    layout, cases = fixed_schedules()
    for name, steps, effects in cases:
        for faults in [
            (None,),
            ('after-undo-log', None),
            ('after-abort', None),
            ('after-redo-write', None),
            ('before-ready', None),
            ('after-undo-log', 'after-redo-write', 'after-abort', None),
            ('after-redo-write', 'after-redo-write', None),
        ]:
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / 'root'
                make_seed(directory, layout)
                schedule = Path(tmp) / 'schedule.json'
                schedule.write_text(json.dumps([to_event(step) for step in steps]))
                code, stdout, stderr = invoke(['main.py', 'script', directory, schedule])
                assert code == 73, (name, code, stdout, stderr)
                rec, exits = recover_until_ready(directory, list(faults), name)
                assert rec['status'] == 'ready'
                wal, _ = check_final(directory, layout, effects, name)
                # Idempotent re-run after READY.
                code, stdout, stderr = invoke(['main.py', 'recover', directory])
                assert code == 0, (name, stdout, stderr)
                before = read_pages(directory)
                code, _, _ = invoke(['main.py', 'recover', directory])
                assert read_pages(directory) == before
                # Recovery may not open business: verify post-recovery reuse works.
                reuse = Path(tmp) / 'reuse.json'
                reuse.write_text(json.dumps([
                    {'op': 'begin', 'tx': 'LATER'},
                    {'op': 'add', 'tx': 'LATER', 'page': 'p0', 'key': 'alpha',
                     'delta': 11},
                    {'op': 'commit', 'tx': 'LATER'},
                    {'op': 'crash'},
                ]))
                code, stdout, stderr = invoke(['main.py', 'script', directory, reuse])
                assert code == 73, (name, stdout, stderr)
                code, stdout, stderr = invoke(['main.py', 'recover', directory])
                assert code == 0, (name, stdout, stderr)
                later_effects = dict(effects)
                later_effects[('p0', 'alpha')] = later_effects.get(('p0', 'alpha'), 0) + 11
                check_final(directory, layout, later_effects, name + '-reuse')
                report.append({'case': name, 'faults': faults, 'exits': exits,
                               'wal_records': len(wal)})


# ---------------------------------------------------------------------------
# Randomized legal schedules
# ---------------------------------------------------------------------------

def random_schedule(rng):
    page_ids = ['p0', 'p1', 'p2']
    layout = {page_id: {'rows': {'k%02d' % i: rng.randint(-1000, 1000)
                                 for i in range(6)}} for page_id in page_ids}
    # Guarantee distinctive values that no generated business ADD may touch.
    layout['p1']['rows']['reserve'] = 12345
    layout['p0']['rows']['untouched'] = 731
    writable_keys = {page_id: [key for key in sorted(spec['rows'])
                               if key not in ('reserve', 'untouched')]
                     for page_id, spec in layout.items()}
    schedule = []
    active = {}                       # tx -> set of owned (page,key)
    owners = {}                       # (page,key) -> tx
    # Independent oracle state: only ADDs actually accepted (no LOCKED, no
    # int64 rejection) are recorded per open transaction; the map is folded
    # into the committed total only when that transaction commits.
    accepted = {}                     # tx -> {(page,key): summed delta}
    adds = 0
    aborts = 0
    next_tx = 0
    committed_effects = {}
    while adds < 100 and aborts < 16:
        roll = rng.random()
        if len(active) < 4 and roll < 0.25:
            tx = 'tx%d' % next_tx
            next_tx += 1
            active[tx] = set()
            accepted[tx] = {}
            schedule.append({'op': 'begin', 'tx': tx})
            continue
        if active and roll < 0.8:
            tx = rng.choice(sorted(active))
            page_id = rng.choice(page_ids)
            key = rng.choice(writable_keys[page_id])
            owner = owners.get((page_id, key))
            if owner is not None and owner != tx:
                # The lock conflict itself is a required, observable rejection.
                event = {'op': 'add', 'tx': tx, 'page': page_id, 'key': key,
                         'delta': 1, 'expected_error': 'LOCKED'}
                schedule.append(event)
                continue
            delta = rng.choice([rng.randint(-50, 50), rng.randint(-50, 50),
                                -1, 1, 100, -100])
            schedule.append({'op': 'add', 'tx': tx, 'page': page_id,
                             'key': key, 'delta': delta})
            active[tx].add((page_id, key))
            owners[(page_id, key)] = tx
            slot = (page_id, key)
            accepted[tx][slot] = accepted[tx].get(slot, 0) + delta
            adds += 1
            continue
        if active:
            tx = rng.choice(sorted(active))
            if rng.random() < 0.5 and aborts < 16:
                schedule.append({'op': 'abort', 'tx': tx})
                aborts += 1
            else:
                schedule.append({'op': 'commit', 'tx': tx})
                for slot, delta in accepted.pop(tx).items():
                    committed_effects[slot] = committed_effects.get(slot, 0) + delta
            active.pop(tx)
            for slot in [slot for slot, owner in owners.items() if owner == tx]:
                del owners[slot]
            if rng.random() < 0.4:
                schedule.append({'op': 'flush', 'page': rng.choice(page_ids)})
    for tx in sorted(active):
        # Open transactions at crash are losers: their accepted ADDs are dropped.
        schedule.append({'op': 'abort', 'tx': tx})
    schedule.append({'op': 'crash'})
    return layout, schedule, committed_effects


def run_random(report):
    for seed in range(20):
        rng = random.Random(9000 + seed)
        layout, schedule, effects = random_schedule(rng)
        assert sum(1 for e in schedule if e['op'] == 'add'
                   and 'expected_error' not in e) <= 100
        assert sum(1 for e in schedule if e['op'] == 'abort') <= 20
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'root'
            make_seed(directory, layout)
            schedule_path = Path(tmp) / 'schedule.json'
            schedule_path.write_text(json.dumps(schedule))
            code, stdout, stderr = invoke(['main.py', 'script', directory, schedule_path])
            assert code == 73, (seed, code, stdout, stderr)
            # Durability evidence from the real writer audit.
            writer = json.loads(stdout)
            for event in writer['engine']:
                if 'durable_lsn_before_page_write' in event:
                    assert event['durable_lsn_before_page_write'] >= event['page_lsn']
            faults = [rng.choice(ALL_FAULTS) for _ in range(rng.randint(1, 4))]
            rec, exits = recover_until_ready(directory, faults, 'fuzz%d' % seed)
            assert rec['status'] == 'ready'
            wal, pages = check_final(directory, layout, effects, 'fuzz%d' % seed)
            assert pages['p1']['rows']['reserve'] == 12345
            assert pages['p0']['rows']['untouched'] == 731
            report.append({'case': 'fuzz%d' % seed, 'faults': faults,
                           'exits': exits, 'business_adds':
                           sum(1 for e in schedule if e['op'] == 'add'
                               and 'expected_error' not in e),
                           'wal_records': len(wal)})


# ---------------------------------------------------------------------------
# Rejection rules and the unrecoverable old directory
# ---------------------------------------------------------------------------

def run_rejections(report):
    layout = {'p0': {'rows': {'alpha': INT64_MAX, 'beta': INT64_MIN, 'gamma': 0}}}
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / 'root'
        make_seed(directory, layout)
        schedule = Path(tmp) / 'schedule.json'
        schedule.write_text(json.dumps([
            {'op': 'begin', 'tx': 'T1'},
            {'op': 'add', 'tx': 'T1', 'page': 'p0', 'key': 'alpha', 'delta': 1,
             'expected_error': 'int64'},
            {'op': 'add', 'tx': 'T1', 'page': 'p0', 'key': 'beta', 'delta': -1,
             'expected_error': 'int64'},
            {'op': 'add', 'tx': 'T1', 'page': 'p0', 'key': 'gamma',
             'delta': True, 'expected_error': 'int64'},
            {'op': 'add', 'tx': 'T1', 'page': 'p0', 'key': 'gamma',
             'delta': 2 ** 63, 'expected_error': 'int64'},
            {'op': 'add', 'tx': 'T1', 'page': 'p0', 'key': 'missing', 'delta': 1,
             'expected_error': 'unknown row'},
            {'op': 'begin', 'tx': 'T2'},
            {'op': 'add', 'tx': 'T2', 'page': 'p0', 'key': 'gamma', 'delta': 5},
            {'op': 'add', 'tx': 'T1', 'page': 'p0', 'key': 'gamma', 'delta': 1,
             'expected_error': 'LOCKED'},
            {'op': 'crash'},
        ]))
        code, stdout, stderr = invoke(['main.py', 'script', directory, schedule])
        assert code == 73, (code, stdout, stderr)
        code, stdout, stderr = invoke(['main.py', 'recover', directory])
        assert code == 0, (stdout, stderr)
        pages = read_pages(directory)
        assert pages['p0']['rows'] == layout['p0']['rows']
        wal = read_wal(directory)
        # Nothing was ever forced (crash before any commit/flush), so the
        # volatile appends vanished with the process: recovery sees an empty
        # WAL and leaves the page exactly at its seed values (NO-FORCE).
        business = [r['kind'] for r in wal
                    if r['kind'] in ('BEGIN', 'ADD', 'UNDO', 'COMMIT', 'ABORT')]
        assert business == [], business
        assert all(r.get('key') != 'missing' for r in wal)
        report.append({'case': 'rejections', 'rows': pages['p0']['rows'],
                       'wal_kinds': [r['kind'] for r in wal]})

    # Duplicate transaction id across processes is rejected once the id has a
    # durable record (here a committed transaction survived in the WAL).
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / 'root'
        make_seed(directory, layout)
        first = Path(tmp) / 'first.json'
        first.write_text(json.dumps([
            {'op': 'begin', 'tx': 'T1'},
            {'op': 'add', 'tx': 'T1', 'page': 'p0', 'key': 'gamma', 'delta': 1},
            {'op': 'commit', 'tx': 'T1'},
            {'op': 'crash'},
        ]))
        code, _, _ = invoke(['main.py', 'script', directory, first])
        assert code == 73
        dup = Path(tmp) / 'dup.json'
        dup.write_text(json.dumps([{'op': 'begin', 'tx': 'T1'}, {'op': 'crash'}]))
        code, stdout, stderr = invoke(['main.py', 'script', directory, dup])
        assert code != 73 and 'already used' in stderr, (code, stdout, stderr)

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / 'root'
        make_seed(directory, layout)
        page_path = directory / 'pages/p0.json'
        broken = json.loads(page_path.read_text())
        broken['page_lsn'] = 42
        broken['rows']['alpha'] = 0
        page_path.write_text(json.dumps(broken))
        code, stdout, stderr = invoke(['main.py', 'recover', directory])
        assert code == 2 and 'beyond the durable WAL end' in stdout
        # Directory untouched after fail-closed.
        assert json.loads(page_path.read_text()) == broken
        code, stdout, stderr = invoke(['main.py', 'inspect', directory])
        assert code == 2 and 'beyond the durable WAL end' in stdout
        report.append({'case': 'fail-closed-gap', 'exit_code': code})


def main():
    report = []
    run_fixed(report)
    run_random(report)
    run_rejections(report)
    print(json.dumps({'status': 'all model checks passed', 'cases': report},
                     indent=2))


if __name__ == '__main__':
    main()
