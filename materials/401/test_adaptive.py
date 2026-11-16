"""Verification harness for the adaptive LEFT JOIN (materials/401).

Not part of the public API.  Run:  python3 test_adaptive.py
It independently enumerates the expected LEFT JOIN, then drives Executor one
``next()`` at a time recording source counts at first visibility, and finally
compares the exact identity sequence.

It also perturbs left/right order, the initial estimate, and the key
distribution, and checks the fanout/sparse cost contract on fresh instances.
"""

import itertools
import json
import random
import sys

from executor import Executor


def oracle(left_rows, right_rows):
    """Direct enumeration in the required order."""
    out = []
    for left in left_rows:
        found = False
        for right in right_rows:
            if (
                left['key'] is not None
                and right['key'] is not None
                and left['key'] == right['key']
            ):
                found = True
                out.append(
                    dict(
                        left_id=left['id'],
                        right_id=right['id'],
                        left_value=left['value'],
                        right_value=right['value'],
                    )
                )
        if not found:
            out.append(
                dict(
                    left_id=left['id'],
                    right_id=None,
                    left_value=left['value'],
                    right_value=None,
                )
            )
    return out


def drive(path, sample_steps=False):
    ex = Executor(path)
    first = ex.next()
    startup = ex.inspect()
    rows = [] if first is None else [first]
    timeline = []
    if first is not None:
        timeline.append((len(rows), dict(startup['source'])))

    while True:
        row = ex.next()
        if row is None:
            break
        rows.append(row)
        if sample_steps and len(rows) <= 8:
            timeline.append((len(rows), dict(ex.inspect()['source'])))
    return rows, startup, ex.inspect(), timeline


def key_of(row):
    return (row['left_id'], row['right_id'])


def check_fixture(path, verbose=True):
    spec = json.load(open(path))
    expected = oracle(spec['left'], spec['right'])
    rows, startup, final, timeline = drive(path, sample_steps=True)

    ok = [key_of(r) for r in rows] == [key_of(e) for e in expected]
    assert ok, f'{path}: identity sequence mismatch'
    # exact values too
    assert rows == expected, f'{path}: value mismatch'

    # no duplicate publication: ids unique per side, so pairs must be unique
    assert len(set(key_of(r) for r in rows)) == len(rows), 'duplicate pair'

    if verbose:
        print(f'{path}')
        print(f'  rows={len(rows)} matches={sum(1 for r in rows if r["right_id"] is not None)}')
        print(f'  first_visible: {rows[0] if rows else None}')
        print(f'  startup source: {startup["source"]}')
        print(f'  final   source: {final["source"]}')
        print(f'  switch: {final["switch"]}')
        print(f'  probes: {final["probes"]}')
        print(f'  timeline (rows_delivered, source): {timeline}')
        okfirst = (
            startup['source']['left_reads'] == 1
            and startup['source']['right_reads'] <= 4
        )
        print(f'  first-visible contract (left=1,right<=4): {okfirst}')
        assert okfirst
        assert final['switch']['switched'], 'no runtime switch'
        assert final['source']['right_reads'] <= 256, 'right_reads > 256'
        assert final['source']['left_opens'] == 1
    return rows, final


def run_perturbations():
    """Random small inputs with varied order/estimate/distribution."""
    rng = random.Random(401)
    for trial in range(300):
        nleft = rng.randint(0, 12)
        nright = rng.randint(0, 16)
        keys = ['a', 'b', 'c', None, 'd']
        left = [
            {'id': f'l{i}', 'key': rng.choice(keys), 'value': rng.randint(0, 9)}
            for i in range(nleft)
        ]
        right = [
            {'id': f'r{j}', 'key': rng.choice(keys), 'value': rng.randint(0, 9)}
            for j in range(nright)
        ]
        spec = {
            'estimated_left_rows': rng.choice([0, 1, 2, 5, 20, 1000]),
            'left': left,
            'right': right,
        }
        path = f'/tmp/perturb_{trial}.json'
        with open(path, 'w') as fh:
            json.dump(spec, fh)
        expected = oracle(left, right)
        rows, startup, final, _ = drive(path)
        assert rows == expected, f'trial {trial} mismatch'
        assert final['source']['left_opens'] == 1
        assert final['source']['left_reads'] == nleft
    print('perturbations: 300 random inputs OK')


def check_handoff_semantics(path):
    """Reproduce the row-boundary ownership claims explicitly."""
    spec = json.load(open(path))
    ex = Executor(path)
    rows = []
    while True:
        r = ex.next()
        if r is None:
            break
        rows.append(r)
    final = ex.inspect()
    # right_reads == first full nested scan + one full build (+ any extra scans)
    build = final['switch']['build_source_right_reads']
    # exactly two right opens => no rescans beyond the initial nested scan+build
    if final['switch']['switched']:
        assert final['source']['right_opens'] == 2, final['source']
        assert build == len(spec['right'])
    print(f'handoff {path}: right_opens={final["source"]["right_opens"]}, '
          f'build_reads={build}, published={len(rows)}')


if __name__ == '__main__':
    failures = 0
    for name in ['normal', 'fanout', 'sparse', 'nulls']:
        path = f'fixtures/{name}.json'
        verbose = name in ('fanout', 'sparse')
        try:
            check_fixture(path, verbose=verbose)
        except AssertionError as e:
            failures += 1
            print(f'FAIL {path}: {e}')
    try:
        run_perturbations()
    except AssertionError as e:
        failures += 1
        print(f'FAIL perturbations: {e}')
    for name in ['fanout', 'sparse']:
        check_handoff_semantics(f'fixtures/{name}.json')
    print('OK' if failures == 0 else f'{failures} FAILURES')
    sys.exit(1 if failures else 0)
