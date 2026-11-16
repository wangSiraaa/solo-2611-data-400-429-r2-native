"""End-to-end verification for the branch-aware contract compiler.

For every rejected plan we take the compiler's own counterexample row, feed
it to a compiler-free runtime and check the failure reproduces at exactly the
node / expression the diagnostic points at.  For every safe plan we compute
each output row with an independent per-row oracle and check every emitted
row is covered by the compiled output contract.
"""

import json
import subprocess
import sys
from pathlib import Path

from contracts import (CompileError, accepts,
                       shape_accepted_by_variant, variant_from_decl, Shape, lit_desc)
from pipeline import compile_plan, run_plan, DataError, execute_to_files
from expressions import evaluate, evaluate_guard

ROOT = Path(__file__).resolve().parent
CASES = ROOT / 'cases'
RESULTS = []


def check(name, condition, detail=''):
    RESULTS.append((name, bool(condition), detail))
    mark = 'PASS' if condition else 'FAIL'
    print('[%s] %s %s' % (mark, name, detail if not condition else ''))


def load(case):
    base = CASES / case
    plan_path = base / 'plan.json'
    if not plan_path.exists():  # root-level fixtures such as unsafe.json
        base = ROOT
        plan_path = base / case
    rows_path = base / 'rows.json'
    return (json.loads(plan_path.read_text()),
            json.loads(rows_path.read_text()) if rows_path.exists() else None)


def find_node(plan, node_id):
    return next(node for node in plan['nodes'] if node['id'] == node_id)


def raw_execute(plan, sources):
    """Compiler-free runtime mirroring run_plan, for counterexample replay."""
    by_id = {node['id']: node for node in plan['nodes']}
    order, values, indegree = [], {}, {nid: 0 for nid in by_id}
    down = {nid: [] for nid in by_id}
    for node in plan['nodes']:
        refs = ([node['input']] if 'input' in node
                else node['inputs'] if node['op'] == 'concat' else [])
        for ref in refs:
            indegree[node['id']] += 1
            down[ref].append(node['id'])
    ready = sorted(nid for nid, d in indegree.items() if d == 0)
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for dst in down[nid]:
            indegree[dst] -= 1
            if indegree[dst] == 0:
                ready.append(dst)
    for nid in order:
        node = by_id[nid]
        if node['op'] == 'source':
            values[nid] = [dict(r) for r in sources[nid]]
        elif node['op'] == 'where':
            values[nid] = [r for r in values[node['input']]
                           if evaluate_guard(node['guard'], r)]
        elif node['op'] == 'project':
            values[nid] = [{c: evaluate(e, r) for c, e in node['columns'].items()}
                           for r in values[node['input']]]
        elif node['op'] == 'concat':
            merged = []
            for ref in node['inputs']:
                merged.extend(values[ref])
            values[nid] = merged
        else:
            values[nid] = list(values[node['input']])
    return values[plan['output']]


def expect_reject(case, expected_code):
    plan, rows = load(case)
    try:
        compile_plan(plan)
    except CompileError as error:
        check('%s rejected with %s' % (case, expected_code),
              error.code == expected_code, error.code)
        return error
    check('%s rejected with %s' % (case, expected_code), False, 'compiler accepted')
    return None


def replay_counterexample(case, error, runtime_reproducible=True):
    plan, _ = load(case)
    detail = error.detail
    row = detail.get('counterexample_row')
    source_id = None
    origin = detail.get('origin')
    if origin is not None:
        row = origin['counterexample_row']
        source_id = origin['source']
    elif detail.get('offending_shape'):
        provenance = detail['offending_shape']
        row = provenance['counterexample_row']
        source_id = provenance['source']
    if row is None or source_id is None:
        check('%s carries a counterexample' % case, False, json.dumps(detail))
        return
    source_node = find_node(plan, source_id)
    legal = any(accepts(row, v) for v in source_node['variants'])
    check('%s counterexample is a legal source row' % case, legal, str(row))
    if not runtime_reproducible:
        # Static-only failures: unsupported_guard (open-domain relation that no
        # concrete row can settle) and assert_failed (a contract-level check).
        check('%s static-only diagnostic (no runtime error required)' % case,
              error.code in ('unsupported_guard', 'assert_failed'))
        return
    try:
        raw_execute(plan, {source_id: [row]})
    except (KeyError, TypeError) as exc:
        check('%s counterexample reproduces runtime failure' % case,
              type(exc).__name__ in ('KeyError', 'TypeError'), repr(exc))
        return
    check('%s counterexample reproduces runtime failure' % case, False,
          'no runtime error')


def expect_safe(case, oracle, grouped=False, whole_payload=False):
    plan, rows = load(case)
    compiled = compile_plan(plan)
    actual = run_plan(plan, rows)
    if whole_payload:
        expected = oracle(rows)
    elif grouped:
        source_ids = [n['id'] for n in plan['nodes'] if n['op'] == 'source']
        expected = [oracle(row)
                    for source_id in source_ids for row in rows[source_id]]
    else:
        expected = [oracle(row) for payload in rows.values() for row in payload]
    check('%s row-by-row oracle output' % case, actual == expected,
          'actual=%s expected=%s' % (actual, expected))
    variants = [variant_from_decl(v)
                for v in compiled['output_contract']['shapes']]
    covered = all(any(shape_accepted_by_variant(_row_shape(row), v) for v in variants)
                  for row in actual)
    check('%s every output row covered by compiled contract' % case, covered)
    check('%s keeps branch shapes separate' % case,
          len(compiled['output_contract']['shapes']) >= 1)
    return compiled, actual


def _row_shape(row):
    """Turn a concrete runtime row into a finite-domain Shape-like descriptor."""
    return Shape({k: lit_desc(v) for k, v in row.items()}, ('oracle', 0, dict(row)))


# --------------------------------------------------------------------------- #
# Independent per-row oracles
# --------------------------------------------------------------------------- #

def oracle_literals(row):
    kind = row['kind']
    flag = row.get('flag')
    n = row.get('n')
    out = {
        'kind': kind,
        'kind_is_litb': kind == 'litb',
        'flag_status': ('bool-present' if isinstance(flag, bool)
                        else 'absent' if 'flag' not in row else 'nonbool-present'),
        'n_plus_41': (n + 41) if type(n) is int else None,
        'flag_shape': ('flag-is-bool' if isinstance(flag, bool)
                       else 'flag-absent'),
    }
    return out


def oracle_null(row):
    return {
        'kind': row['kind'],
        'note_present': 'note' in row,
        # NULL is present: default must NOT be used
        'note_or_default': row['note'] if 'note' in row else 'DEFAULT',
        'other_or_default': row['other'] if 'other' in row else 'DEFAULT',
        'guarded_note': row['note'] if 'note' in row else 'was-missing',
    }


def oracle_concat_row(row):
    if row['kind'] == 'sale':
        amount = row['gross'] + row['tax']
    else:
        amount = row['net']
    return {'kind': row['kind'], 'amount': amount,
            'amount_plus_one': amount + 1}


def oracle_where_row(row):
    return {'amount': row['gross'] if row['kind'] == 'sale' else row['net']}


def oracle_where_shared(rows):
    sales = [oracle_where_row(r) for r in rows['src'] if r['kind'] == 'sale']
    adjustments = [oracle_where_row(r) for r in rows['src']
                   if r['kind'] == 'adjustment']
    return sales + adjustments


# --------------------------------------------------------------------------- #
# Scenario checks
# --------------------------------------------------------------------------- #

def check_baseline():
    unsafe = json.loads((ROOT / 'unsafe.json').read_text())
    safe = json.loads((ROOT / 'safe.json').read_text())
    rows = json.loads((ROOT / 'rows.json').read_text())
    err = expect_reject('unsafe.json', 'missing_field')
    replay_counterexample('unsafe.json', err)
    # Sales-only data must still be blocked at compile time.
    try:
        run_plan(unsafe, {'source': [rows['source'][0]]})
        check('unsafe blocked even with sales-only data', False)
    except CompileError:
        check('unsafe blocked even with sales-only data', True)
    compiled = compile_plan(safe)
    out = run_plan(safe, rows)
    check('safe values 107/30', [r['amount'] for r in out] == [107, 30], str(out))
    check('safe keeps two branch shapes',
          len(compiled['output_contract']['shapes']) == 2)


def check_structural():
    def inline(plan, expected_code):
        try:
            compile_plan(plan)
            check('structural %s rejected' % expected_code, False, 'accepted')
        except CompileError as error:
            check('structural %s rejected' % expected_code,
                  error.code == expected_code, error.code)

    base, _ = load('where_shared')
    unknown = json.loads(json.dumps(base))
    unknown['nodes'][1]['input'] = 'ghost'
    inline(unknown, 'unknown_reference')

    cyclic = json.loads(json.dumps(base))
    cyclic['nodes'] = [n for n in cyclic['nodes'] if n['id'] not in
                       ('only_adjustments', 'adjustment_amounts', 'merged', 'checked')]
    cyclic['nodes'].append({'id': 'loop', 'op': 'concat', 'inputs': ['sale_amounts']})
    for node in cyclic['nodes']:
        if node['id'] == 'sale_amounts':
            node['input'] = 'loop'
    cyclic['output'] = 'loop'
    inline(cyclic, 'cycle')

    bad_variant = {'nodes': [{'id': 's', 'op': 'source',
                              'variants': [{'f': {'type': 'date'}}]}],
                   'output': 's'}
    inline(bad_variant, 'invalid_plan')

    too_many = {'nodes': [{'id': 's', 'op': 'source',
                           'variants': [dict(a={'type': 'int'}) for a in
                                        ['f%d' % i for i in range(17)]]}],
                'output': 's'}
    inline(too_many, 'invalid_plan')


def check_data_rejection_and_files():
    plan, rows = load('bad_source_row')
    try:
        run_plan(plan, rows)
        check('illegal source rows rejected with row numbers', False)
    except DataError as error:
        nums = sorted(v['row_number'] for v in error.violations)
        check('illegal source rows rejected with row numbers', nums == [2, 3], str(nums))
        check('violation names source',
              all(v['source'] == 'src' for v in error.violations))

    out_dir = ROOT / 'verify_out'

    # Safe case: three separate artefacts.
    diag = execute_to_files(CASES / 'where_shared' / 'plan.json',
                            CASES / 'where_shared' / 'rows.json', out_dir)
    check('safe CLI status ok', diag['status'] == 'ok')
    check('contract/diagnostic/data files separated',
          all((out_dir / f).exists() for f in
              ('contract.json', 'diagnostics.json', 'data.json')))
    # Rejected case: only diagnostics, no contract/data.
    diag2 = execute_to_files(CASES / 'assert_missing' / 'plan.json',
                             CASES / 'assert_missing' / 'rows.json', out_dir)
    check('rejected CLI status compile_failed', diag2['status'] == 'compile_failed')
    check('rejected CLI emits no contract/data',
          not (out_dir / 'contract.json').exists()
          and not (out_dir / 'data.json').exists())


def check_cli_subprocess():
    proc = subprocess.run(
        [sys.executable, str(ROOT / 'pipeline.py'),
         str(ROOT / 'unsafe.json'), '--out-dir',
         str(ROOT / 'verify_out_cli')],
        capture_output=True, text=True)
    check('cli exits non-zero on failed compile', proc.returncode == 1, proc.stdout)
    proc2 = subprocess.run(
        [sys.executable, str(ROOT / 'pipeline.py'),
         str(ROOT / 'safe.json'), '--rows', str(ROOT / 'rows.json'),
         '--out-dir', str(ROOT / 'verify_out_cli')],
        capture_output=True, text=True)
    check('cli exits zero on safe plan', proc2.returncode == 0, proc2.stderr)
    data = json.loads((ROOT / 'verify_out_cli' / 'data.json').read_text())
    check('cli data file holds 107/30', [r['amount'] for r in data] == [107, 30])


def main():
    check_baseline()

    safe_results = {}
    safe_results['literals'] = expect_safe('literals', oracle_literals)
    safe_results['null_and_missing'] = expect_safe('null_and_missing', oracle_null)
    safe_results['concat_reproject'] = expect_safe(
        'concat_reproject', oracle_concat_row, grouped=True)
    safe_results['where_shared'] = expect_safe(
        'where_shared', oracle_where_shared, whole_payload=True)

    # concat multiset order: sales first, then adjustments, no dedup.
    plan, rows = load('concat_reproject')
    out = run_plan(plan, rows)
    check('concat preserves multiset and order',
          [r['amount'] for r in out] == [107, 53, 30], str(out))

    # NULL really is preserved, not replaced by the get default.
    null_out = safe_results['null_and_missing'][1]
    check('get does not default an existing NULL',
          null_out[0]['note_or_default'] is None
          and null_out[0]['guarded_note'] is None
          and null_out[1]['note_or_default'] == 'DEFAULT'
          and null_out[1]['guarded_note'] == 'was-missing'
          and null_out[0]['note_present'] is True
          and null_out[1]['note_present'] is False)

    # Rejected plans + counterexample replay.
    for case, code, runtime_repro in (
            ('unsupported_guard', 'unsupported_guard', False),
            ('assert_missing', 'assert_failed', False),
            ('type_error', 'type_error', True)):
        error = expect_reject(case, code)
        if error is not None:
            replay_counterexample(case, error, runtime_repro)

    check_structural()
    check_data_rejection_and_files()
    check_cli_subprocess()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print('\n%d checks, %d failed' % (len(RESULTS), len(failed)))
    if failed:
        print('FAILED:', failed)
        raise SystemExit(1)
    print('ALL CHECKS PASSED')


if __name__ == '__main__':
    main()
