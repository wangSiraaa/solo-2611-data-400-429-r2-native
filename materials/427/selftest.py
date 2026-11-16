#!/usr/bin/env python3
"""Self-test: shared bounded-state executor vs an independent per-check-scan oracle.

The oracle below scans each source independently per check (the wasteful but
obviously-correct approach) and is used only to verify the shared executor's
counts and witnesses; it is never part of the executed plan.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import compiler
import engine

FAILED = []


def check(name, condition, info=''):
    if condition:
        print('ok   %s' % name)
    else:
        FAILED.append(name)
        print('FAIL %s  %s' % (name, info))


# ---------------- independent per-check-scan oracle ----------------

def o_type(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int):
        return 'int'
    if isinstance(value, str):
        return 'text'
    raise ValueError('non-leaf')


def o_rank(value):
    return {'null': 0, 'bool': 1, 'int': 2, 'text': 3}[o_type(value)]


def o_eq(left, right):
    return o_type(left) == o_type(right) and left == right


def o_match(row, pred):
    op = pred['op']
    if op == 'all':
        return True
    if op == 'eq':
        return o_eq(row.get(pred['field']), pred['value'])
    if op == 'in':
        return any(o_eq(row.get(pred['field']), v) for v in pred['values'])
    if op == 'is_null':
        return row.get(pred['field']) is None
    if op == 'and':
        return all(o_match(row, a) for a in pred['args'])
    if op == 'or':
        return any(o_match(row, a) for a in pred['args'])
    if op == 'not':
        return not o_match(row, pred['arg'])
    raise ValueError('unknown op')


def o_typed(value):
    return {'type': o_type(value), 'value': value}


def o_read(base, spec):
    path = Path(base) / spec['file']
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def o_sort_ids(row_ids):
    return sorted(row_ids, key=lambda rid: (o_rank(rid), rid))


def oracle_check(base, catalog, check):
    """Fully independent: reads the source (and target) files per check."""
    src = catalog['sources'][check['source']]
    rows = o_read(base, src)
    pop = [r for r in rows if o_match(r, check['population'])]
    cid, kind = check['check_id'], check['kind']
    path = '/checks/%s' % cid
    population_count = len(pop)
    if kind == 'not_null':
        bad = [r['row_id'] for r in pop if r.get(check['field']) is None]
        witnesses = [{'assertion': 'not_null', 'check_id': cid, 'source': check['source'],
                      'row_id': rid, 'path': path, 'field': check['field']}
                     for rid in o_sort_ids(bad)[:3]]
        return {'population_count': population_count, 'evaluated_count': population_count,
                'violation_count': len(bad), 'passed': not bad, 'witnesses': witnesses}
    if kind == 'unique':
        fields, nulls = check['fields'], check.get('nulls', 'equal')
        groups = {}
        evaluated = 0
        for row in pop:
            parts = [row.get(f) for f in fields]
            if nulls == 'ignore' and any(p is None for p in parts):
                continue
            evaluated += 1
            groups.setdefault(tuple((o_type(p), p) for p in parts), []).append(row['row_id'])
        dup = {k: ids for k, ids in groups.items() if len(ids) > 1}
        bad = [rid for ids in dup.values() for rid in ids]
        key_by_row = {rid: k for k, ids in dup.items() for rid in ids}
        witnesses = []
        for rid in o_sort_ids(bad)[:3]:
            k = key_by_row[rid]
            partner = o_sort_ids([x for x in dup[k] if x != rid])[0]
            witnesses.append({'assertion': 'unique', 'check_id': cid, 'source': check['source'],
                              'row_id': rid, 'path': path, 'fields': list(fields),
                              'key': [o_typed(v) for _t, v in k], 'partner': partner})
        return {'population_count': population_count, 'evaluated_count': evaluated,
                'violation_count': len(bad), 'passed': not bad, 'witnesses': witnesses}
    # reference
    a = check
    tspec = catalog['sources'][a['target_source']]
    tkeys = set()
    for row in o_read(base, tspec):
        if o_match(row, a['target_population']):
            parts = [row.get(f) for f in a['target_fields']]
            if any(p is None for p in parts):
                continue
            tkeys.add(tuple((o_type(p), p) for p in parts))
    nulls = a.get('nulls', 'ignore')
    evaluated = 0
    bad = []
    for row in pop:
        parts = [row.get(f) for f in a['source_fields']]
        if any(p is None for p in parts):
            if nulls == 'ignore':
                continue
            evaluated += 1
            bad.append((row['row_id'], parts))
            continue
        evaluated += 1
        if tuple((o_type(p), p) for p in parts) not in tkeys:
            bad.append((row['row_id'], parts))
    bad.sort(key=lambda item: (o_rank(item[0]), item[0]))
    witnesses = [{'assertion': 'reference', 'check_id': cid, 'source': check['source'],
                  'row_id': rid, 'path': path, 'target_source': a['target_source'],
                  'key': {'fields': list(a['source_fields']),
                          'values': [o_typed(p) for p in parts]}}
                 for rid, parts in bad[:3]]
    return {'population_count': population_count, 'evaluated_count': evaluated,
            'violation_count': len(bad), 'passed': not bad, 'witnesses': witnesses}


def oracle(base, catalog, checks):
    return {c['check_id']: oracle_check(base, catalog, c) for c in checks}


# ---------------- harness ----------------

def write_case(directory, catalog, checks, sources):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in sources.items():
        with (directory / catalog['sources'][name]['file']).open('w') as out:
            for row in rows:
                out.write(json.dumps(row) + '\n')
    (directory / 'catalog.json').write_text(json.dumps(catalog))
    (directory / 'checks.json').write_text(json.dumps(checks))
    return directory


def run_and_compare(name, catalog, checks, sources, chunk_size=8, extra=None):
    with tempfile.TemporaryDirectory() as tmp:
        base = write_case(Path(tmp) / 'in', catalog, checks, sources)
        result = engine.execute_plan(catalog, checks, base_dir=base, out_dir=Path(tmp) / 'out',
                                     chunk_size=chunk_size)
        check(name + '/status', result['status'] == 'complete',
              json.dumps(result.get('error')))
        expected = oracle(base, catalog, checks)
        by_id = {c['check_id']: c for c in result['checks']}
        for cid, exp in expected.items():
            got = by_id[cid]
            for field in ('population_count', 'evaluated_count', 'violation_count', 'passed'):
                check('%s/%s/%s' % (name, cid, field), got[field] == exp[field],
                      'got %r want %r' % (got[field], exp[field]))
            check('%s/%s/witnesses' % (name, cid), got['witnesses'] == exp['witnesses'],
                      'got %s want %s' % (json.dumps(got['witnesses']), json.dumps(exp['witnesses'])))
        involved = {c['source'] for c in checks} | {c['target_source'] for c in checks
                                                    if c['kind'] == 'reference'}
        for source_name in involved:
            counter = result['sources'][source_name]
            check('%s/%s/one_shared_scan' % (name, source_name), counter['scans'] == 1,
                  'scans=%r' % counter['scans'])
            check('%s/%s/decode_all_rows_once' % (name, source_name),
                  counter['decoded'] == len(sources[source_name]),
                  'decoded=%r rows=%r' % (counter['decoded'], len(sources[source_name])))
        check(name + '/resident_budget', result['resident']['peak_rows'] <= 64,
              json.dumps(result['resident']))
        check(name + '/page_cache_disclosed',
              result['sqlite']['page_cache_bytes'] == 4 * 1024 * 1024, '')
        if extra:
            extra(result, base)
        return result


# ---------------- scenarios ----------------

def test_materials():
    catalog = json.loads((ROOT / 'catalog.json').read_text())
    checks = json.loads((ROOT / 'checks.json').read_text())
    with tempfile.TemporaryDirectory() as tmp:
        result = engine.execute_plan(catalog, checks, base_dir=ROOT, out_dir=Path(tmp))
        by_id = {c['check_id']: c for c in result['checks']}
        a, b = by_id['a-email'], by_id['b-email-unique']
        check('materials/status', result['status'] == 'complete' and result['all_pass'] is True, '')
        check('materials/A_denominator_is_1', a['population_count'] == 1, str(a))
        check('materials/A_email_passes', a['passed'] is True and a['violation_count'] == 0, str(a))
        check('materials/B_denominator_is_2', b['population_count'] == 2, str(b))
        check('materials/B_unique_passes', b['passed'] is True and b['evaluated_count'] == 2, str(b))
        check('materials/one_scan', result['sources']['events']['scans'] == 1
              and result['sources']['events']['decoded'] == 3, '')
        expected = oracle(ROOT, catalog, checks)
        for cid, exp in expected.items():
            got = by_id[cid]
            check('materials/%s/matches_oracle' % cid,
                  all(got[f] == exp[f] for f in
                      ('population_count', 'evaluated_count', 'violation_count', 'passed'))
                  and got['witnesses'] == exp['witnesses'], json.dumps(got))


def test_probe_and_benchmark_scripts():
    probe = subprocess.run([sys.executable, 'probe.py'], cwd=ROOT, capture_output=True, text=True)
    check('scripts/probe_runs', probe.returncode == 0, probe.stderr)
    if probe.returncode == 0:
        out = json.loads(probe.stdout)
        check('scripts/probe_fused_equals_oracle',
              out['fused_actual'] == out['independent_per_check_oracle'], probe.stdout)
        check('scripts/probe_normal_path',
              out['normal_same_population'][0]['population_count'] == 1
              and out['normal_same_population'][1]['population_count'] == 1, '')
        check('scripts/probe_one_scan', out['source_scans'] == 1 and out['source_decodes'] == 3, '')
    bench = subprocess.run([sys.executable, 'benchmark_probe.py'], cwd=ROOT,
                           capture_output=True, text=True)
    check('scripts/benchmark_probe_runs', bench.returncode == 0, bench.stderr)


def test_cli_on_materials():
    with tempfile.TemporaryDirectory() as tmp:
        compile_run = subprocess.run([sys.executable, 'dq.py', 'compile', '--input', str(ROOT),
                                      '--out', tmp], cwd=ROOT, capture_output=True, text=True)
        check('cli/compile_exit', compile_run.returncode == 0, compile_run.stderr)
        plan = json.loads(compile_run.stdout)
        check('cli/compile_one_shared_scan', len(plan['scans']) == 1
              and plan['scans'][0]['read_columns'] == ['email', 'row_id', 'team'], '')
        check('cli/compile_boundaries',
              all('responsibility' in c for c in plan['checks'])
              and all('responsibility' in s['consumers'][0] for s in plan['scans']), '')
        run = subprocess.run([sys.executable, 'dq.py', 'run', '--input', str(ROOT),
                              '--out', tmp], cwd=ROOT, capture_output=True, text=True)
        check('cli/run_exit', run.returncode == 0, run.stderr)
        inspect = subprocess.run([sys.executable, 'dq.py', 'inspect', '--input', str(ROOT),
                                  '--out', tmp], cwd=ROOT, capture_output=True, text=True)
        check('cli/inspect_exit', inspect.returncode == 0, inspect.stderr)
        check('cli/inspect_counters', 'scans=1 decoded=3' in inspect.stdout
              and 'population=1' in inspect.stdout and 'population=2' in inspect.stdout,
              inspect.stdout)


def test_cross_batch_duplicates():
    catalog = {'sources': {'s': {'file': 's.jsonl', 'schema': {
        'row_id': 'text', 'code': 'int', 'email': ['text', 'null']}}}}
    rows = []
    for i in range(60):
        rows.append({'row_id': 'r%02d' % i, 'code': i,
                     'email': None if i % 7 == 0 else 'e%d' % i})
    rows[5]['code'] = rows[41]['code'] = 900          # duplicate across read batches
    rows[10]['code'] = rows[30]['code'] = rows[50]['code'] = 901  # group of 3 -> 3 rows
    checks = [
        {'check_id': 'u', 'kind': 'unique', 'source': 's', 'fields': ['code'],
         'population': {'op': 'all'}, 'nulls': 'equal'},
        {'check_id': 'n', 'kind': 'not_null', 'source': 's', 'field': 'email',
         'population': {'op': 'all'}},
    ]

    def extra(result, _base):
        u = {c['check_id']: c for c in result['checks']}['u']
        check('cross_batch/violation_total', u['violation_count'] == 5, str(u))
        check('cross_batch/witness_ids',
              [w['row_id'] for w in u['witnesses']] == ['r05', 'r10', 'r30'], str(u['witnesses']))
        check('cross_batch/partners',
              [w['partner'] for w in u['witnesses']] == ['r41', 'r30', 'r10'],
              str(u['witnesses']))
    run_and_compare('cross_batch', catalog, checks, {'s': rows}, chunk_size=4, extra=extra)


def test_two_references_shared_target():
    catalog = {'sources': {
        'events': {'file': 'events.jsonl', 'schema': {
            'row_id': 'text', 'product': 'text', 'qty': 'int'}},
        'products': {'file': 'products.jsonl', 'schema': {
            'row_id': 'text', 'product': 'text', 'active': 'bool'}}}}
    events = [{'row_id': 'e1', 'product': 'p1', 'qty': 1},
              {'row_id': 'e2', 'product': 'p2', 'qty': 2},
              {'row_id': 'e3', 'product': 'p9', 'qty': 3},
              {'row_id': 'e4', 'product': 'p3', 'qty': 4}]
    products = [{'row_id': 'p1', 'product': 'p1', 'active': True},
                {'row_id': 'p2', 'product': 'p2', 'active': False},
                {'row_id': 'p3', 'product': 'p3', 'active': True}]
    checks = [
        {'check_id': 'ref_active', 'kind': 'reference', 'source': 'events',
         'population': {'op': 'all'}, 'target_source': 'products',
         'target_population': {'op': 'eq', 'field': 'active', 'value': True},
         'source_fields': ['product'], 'target_fields': ['product'], 'nulls': 'ignore'},
        {'check_id': 'ref_all', 'kind': 'reference', 'source': 'events',
         'population': {'op': 'all'}, 'target_source': 'products',
         'target_population': {'op': 'all'},
         'source_fields': ['product'], 'target_fields': ['product'], 'nulls': 'ignore'},
    ]

    def extra(result, _base):
        by_id = {c['check_id']: c for c in result['checks']}
        check('two_refs/active_misses', by_id['ref_active']['violation_count'] == 2, '')
        check('two_refs/all_misses', by_id['ref_all']['violation_count'] == 1, '')
        check('two_refs/target_scanned_once', result['sources']['products']['scans'] == 1, '')
        keys = [[v['value'] for v in w['key']['values']] for w in by_id['ref_active']['witnesses']]
        check('two_refs/typed_keys', keys == [['p2'], ['p9']], str(keys))
    run_and_compare('two_refs', catalog, checks, {'events': events, 'products': products},
                    extra=extra)


def test_target_duplicate_keys():
    catalog = {'sources': {
        'events': {'file': 'events.jsonl', 'schema': {'row_id': 'text', 'product': 'text'}},
        'products': {'file': 'products.jsonl', 'schema': {'row_id': 'text', 'product': 'text'}}}}
    events = [{'row_id': 'e1', 'product': 'p1'}, {'row_id': 'e2', 'product': 'p2'},
              {'row_id': 'e3', 'product': 'pX'}]
    products = [{'row_id': 'a', 'product': 'p1'}, {'row_id': 'b', 'product': 'p1'},
                {'row_id': 'c', 'product': 'p2'}, {'row_id': 'd', 'product': 'p2'},
                {'row_id': 'e', 'product': 'p3'}]
    checks = [{'check_id': 'ref', 'kind': 'reference', 'source': 'events',
               'population': {'op': 'all'}, 'target_source': 'products',
               'target_population': {'op': 'all'},
               'source_fields': ['product'], 'target_fields': ['product'], 'nulls': 'ignore'}]

    def extra(result, _base):
        ref = {c['check_id']: c for c in result['checks']}['ref']
        check('target_dups/no_amplification', ref['violation_count'] == 1, str(ref))
        target = ref['detail']['target']
        check('target_dups/stats', target == {'matched_rows': 5, 'null_key_rows_excluded': 0,
                                              'distinct_keys': 3, 'duplicate_key_rows': 2},
              str(target))
    run_and_compare('target_dups', catalog, checks, {'events': events, 'products': products},
                    extra=extra)


def test_all_ignored_and_empty():
    catalog = {'sources': {
        'events': {'file': 'events.jsonl', 'schema': {
            'row_id': 'text', 'email': ['text', 'null'], 'ref': ['text', 'null']}},
        'products': {'file': 'products.jsonl', 'schema': {'row_id': 'text', 'product': 'text'}},
        'empty': {'file': 'empty.jsonl', 'schema': {'row_id': 'text', 'v': 'text'}}}}
    events = [{'row_id': 'e%d' % i, 'email': None, 'ref': None} for i in range(5)]
    products = [{'row_id': 'p1', 'product': 'p1'}]
    checks = [
        {'check_id': 'u_ignored', 'kind': 'unique', 'source': 'events', 'fields': ['email'],
         'population': {'op': 'all'}, 'nulls': 'ignore'},
        {'check_id': 'r_ignored', 'kind': 'reference', 'source': 'events',
         'population': {'op': 'all'}, 'target_source': 'products',
         'target_population': {'op': 'all'}, 'source_fields': ['ref'],
         'target_fields': ['product'], 'nulls': 'ignore'},
        {'check_id': 'n_empty', 'kind': 'not_null', 'source': 'empty', 'field': 'v',
         'population': {'op': 'all'}},
        {'check_id': 'u_empty', 'kind': 'unique', 'source': 'empty', 'fields': ['v'],
         'population': {'op': 'all'}, 'nulls': 'equal'},
        {'check_id': 'r_empty_source', 'kind': 'reference', 'source': 'empty',
         'population': {'op': 'all'}, 'target_source': 'products',
         'target_population': {'op': 'all'}, 'source_fields': ['v'],
         'target_fields': ['product'], 'nulls': 'ignore'},
        {'check_id': 'r_empty_target', 'kind': 'reference', 'source': 'products',
         'population': {'op': 'all'}, 'target_source': 'empty',
         'target_population': {'op': 'all'}, 'source_fields': ['product'],
         'target_fields': ['v'], 'nulls': 'ignore'},
    ]

    def extra(result, _base):
        by_id = {c['check_id']: c for c in result['checks']}
        u = by_id['u_ignored']
        check('ignored/population_kept', u['population_count'] == 5
              and u['evaluated_count'] == 0 and u['violation_count'] == 0
              and u['passed'] is True, str(u))
        r = by_id['r_ignored']
        check('ignored/reference', r['population_count'] == 5 and r['evaluated_count'] == 0
              and r['passed'] is True, str(r))
        e = by_id['n_empty']
        check('empty/counts', e['population_count'] == 0 and e['evaluated_count'] == 0
              and e['violation_count'] == 0 and e['passed'] is True, str(e))
        t = by_id['r_empty_target']
        check('empty/target_all_miss', t['violation_count'] == 1 and t['passed'] is False, str(t))
        check('empty/decoded', result['sources']['empty']['decoded'] == 0
              and result['sources']['empty']['scans'] == 1, '')
    run_and_compare('ignored_empty', catalog, checks,
                    {'events': events, 'products': products, 'empty': []}, extra=extra)


def test_typed_values():
    catalog = {'sources': {'t': {'file': 't.jsonl', 'schema': {
        'row_id': 'text', 'v': ['null', 'bool', 'int', 'text']}}}}
    values = [None, '', True, 1, None, '', True, 1, '1', 0, False]
    rows = [{'row_id': 'r%d' % (i + 1), 'v': v} for i, v in enumerate(values)]
    checks = [
        {'check_id': 'u_equal', 'kind': 'unique', 'source': 't', 'fields': ['v'],
         'population': {'op': 'all'}, 'nulls': 'equal'},
        {'check_id': 'u_ignore', 'kind': 'unique', 'source': 't', 'fields': ['v'],
         'population': {'op': 'all'}, 'nulls': 'ignore'},
        {'check_id': 'nn_all', 'kind': 'not_null', 'source': 't', 'field': 'v',
         'population': {'op': 'all'}},
        {'check_id': 'nn_empty_str', 'kind': 'not_null', 'source': 't', 'field': 'v',
         'population': {'op': 'eq', 'field': 'v', 'value': ''}},
        {'check_id': 'u_bool_int', 'kind': 'unique', 'source': 't', 'fields': ['v'],
         'population': {'op': 'in', 'field': 'v', 'values': [True, 1]}, 'nulls': 'equal'},
        {'check_id': 'nn_is_null', 'kind': 'not_null', 'source': 't', 'field': 'v',
         'population': {'op': 'is_null', 'field': 'v'}},
        {'check_id': 'nn_not_null', 'kind': 'not_null', 'source': 't', 'field': 'v',
         'population': {'op': 'not', 'arg': {'op': 'is_null', 'field': 'v'}}},
        {'check_id': 'u_true_only', 'kind': 'unique', 'source': 't', 'fields': ['v'],
         'population': {'op': 'eq', 'field': 'v', 'value': True}, 'nulls': 'equal'},
        {'check_id': 'nn_int_one', 'kind': 'not_null', 'source': 't', 'field': 'v',
         'population': {'op': 'eq', 'field': 'v', 'value': 1}},
    ]

    def extra(result, _base):
        by_id = {c['check_id']: c for c in result['checks']}
        check('typed/null_is_a_value', by_id['u_equal']['violation_count'] == 8, str(by_id['u_equal']))
        check('typed/ignore_skips_nulls', by_id['u_ignore']['evaluated_count'] == 9
              and by_id['u_ignore']['violation_count'] == 6, str(by_id['u_ignore']))
        check('typed/empty_string_not_null', by_id['nn_empty_str']['population_count'] == 2
              and by_id['nn_empty_str']['violation_count'] == 0, '')
        check('typed/true_not_1', by_id['u_true_only']['population_count'] == 2
              and by_id['nn_int_one']['population_count'] == 2, '')
        check('typed/null_witnesses',
              [w['row_id'] for w in by_id['nn_all']['witnesses']] == ['r1', 'r5'],
              str(by_id['nn_all']['witnesses']))
    run_and_compare('typed', catalog, checks, {'t': rows}, extra=extra)


def test_benchmark_1000():
    catalog = {'sources': {'bench': {'file': 'bench.jsonl', 'schema': {
        'row_id': 'text', 'team': 'text', 'code': 'int', 'email': ['text', 'null']}}}}
    rows = [{'row_id': '%04d' % n, 'team': 'ABC'[n % 3], 'code': n % 41,
             'email': None if n % 17 == 0 else 'email-%d' % n} for n in range(1000)]
    checks = [
        {'check_id': 'A_not_null', 'kind': 'not_null', 'source': 'bench', 'field': 'email',
         'population': {'op': 'eq', 'field': 'team', 'value': 'A'}},
        {'check_id': 'B_unique', 'kind': 'unique', 'source': 'bench', 'fields': ['code'],
         'population': {'op': 'eq', 'field': 'team', 'value': 'B'}, 'nulls': 'equal'},
        {'check_id': 'C_reference_A', 'kind': 'reference', 'source': 'bench',
         'population': {'op': 'eq', 'field': 'team', 'value': 'C'},
         'target_source': 'bench',
         'target_population': {'op': 'eq', 'field': 'team', 'value': 'A'},
         'source_fields': ['code'], 'target_fields': ['code'], 'nulls': 'ignore'},
    ]

    def extra(result, _base):
        by_id = {c['check_id']: c for c in result['checks']}
        check('benchmark/decode_limit', result['sources']['bench']['decoded'] <= 2000,
              str(result['sources']['bench']))
        check('benchmark/expected_violations',
              (by_id['A_not_null']['violation_count'], by_id['B_unique']['violation_count'],
               by_id['C_reference_A']['violation_count']) == (20, 333, 0),
              str({k: v['violation_count'] for k, v in by_id.items()}))
        check('benchmark/populations',
              (by_id['A_not_null']['population_count'], by_id['B_unique']['population_count'],
               by_id['C_reference_A']['population_count']) == (334, 333, 333), '')
    run_and_compare('benchmark', catalog, checks, {'bench': rows}, extra=extra)


def test_config_errors():
    base_catalog = {'sources': {'s': {'file': 's.jsonl', 'schema': {
        'row_id': 'text', 'team': 'text', 'email': ['text', 'null'], 'code': 'int'}}}}
    base_check = {'check_id': 'ok', 'kind': 'not_null', 'source': 's', 'field': 'email',
                  'population': {'op': 'all'}}
    broken = {
        'unknown_field': [{**base_check, 'population': {'op': 'eq', 'field': 'nope', 'value': 1}}],
        'null_literal': [{**base_check, 'population': {'op': 'eq', 'field': 'email', 'value': None}}],
        'float_literal': [{**base_check, 'population': {'op': 'eq', 'field': 'code', 'value': 1.5}}],
        'incompatible_literal': [{**base_check, 'population': {'op': 'eq', 'field': 'team', 'value': 1}}],
        'unknown_op': [{**base_check, 'population': {'op': 'regex', 'field': 'team', 'value': 'a'}}],
        'duplicate_check_id': [base_check, {**base_check}],
        'bad_unique_nulls': [{'check_id': 'u', 'kind': 'unique', 'source': 's', 'fields': ['email'],
                              'population': {'op': 'all'}, 'nulls': 'bogus'}],
        'reference_length_mismatch': [{'check_id': 'r', 'kind': 'reference', 'source': 's',
                                       'population': {'op': 'all'}, 'target_source': 's',
                                       'target_population': {'op': 'all'},
                                       'source_fields': ['email', 'team'], 'target_fields': ['team'],
                                       'nulls': 'ignore'}],
        'unknown_source': [{**base_check, 'source': 'ghost'}],
        'unknown_kind': [{'check_id': 'x', 'kind': 'checksum', 'source': 's',
                          'population': {'op': 'all'}}],
        'empty_in': [{**base_check, 'population': {'op': 'in', 'field': 'team', 'values': []}}],
        'not_missing_arg': [{**base_check, 'population': {'op': 'not'}}],
        'is_null_unknown_field': [{**base_check, 'population': {'op': 'is_null', 'field': 'nope'}}],
        'missing_population': [{'check_id': 'x', 'kind': 'not_null', 'source': 's', 'field': 'email'}],
        'nulls_reject_on_unique': [{'check_id': 'u', 'kind': 'unique', 'source': 's',
                                    'fields': ['email'], 'population': {'op': 'all'},
                                    'nulls': 'reject'}],
    }
    for name, checks in broken.items():
        with tempfile.TemporaryDirectory() as tmp:
            base = write_case(Path(tmp) / 'in', base_catalog, checks,
                              {'s': [{'row_id': 'r1', 'team': 'A', 'email': None, 'code': 1}]})
            result = engine.execute_plan(base_catalog, checks, base_dir=base,
                                         out_dir=Path(tmp) / 'out')
            check('config/%s_incomplete' % name, result['status'] == 'incomplete'
                  and result['error']['phase'] == 'compile', json.dumps(result.get('error')))
            check('config/%s_nothing_passed' % name,
                  all(c['passed'] is not True for c in result['checks']), '')
            check('config/%s_no_reads' % name, result['sources'] == {}, str(result['sources']))
    with tempfile.TemporaryDirectory() as tmp:
        base = write_case(Path(tmp) / 'in', base_catalog, broken['unknown_field'],
                          {'s': [{'row_id': 'r1', 'team': 'A', 'email': None, 'code': 1}]})
        proc = subprocess.run([sys.executable, 'dq.py', 'compile', '--input', str(base),
                               '--out', str(Path(tmp) / 'out')], cwd=ROOT,
                              capture_output=True, text=True)
        check('config/cli_compile_rejects', proc.returncode == 2
              and 'unknown field' in proc.stdout, proc.stdout + proc.stderr)


def test_corrupted_sources():
    catalog = {'sources': {
        'events': {'file': 'events.jsonl', 'schema': {
            'row_id': 'text', 'team': 'text', 'email': ['text', 'null']}},
        'products': {'file': 'products.jsonl', 'schema': {'row_id': 'text', 'product': 'text'}}}}
    checks = [
        {'check_id': 'nn', 'kind': 'not_null', 'source': 'events', 'field': 'email',
         'population': {'op': 'all'}},
        {'check_id': 'uq', 'kind': 'unique', 'source': 'products', 'fields': ['product'],
         'population': {'op': 'all'}, 'nulls': 'equal'},
    ]
    good_events = [{'row_id': 'e1', 'team': 'A', 'email': 'x'},
                   {'row_id': 'e2', 'team': 'B', 'email': None}]
    good_products = [{'row_id': 'p1', 'product': 'p1'}]
    variants = {
        'bad_json': ('{"row_id": "e1", "team": "A", "email": "x"}\n{bad json\n', None),
        'wrong_type': (None, [{'row_id': 'e1', 'team': 7, 'email': 'x'}]),
        'duplicate_row_id': (None, good_events + [dict(good_events[0])]),
        'missing_row_id': (None, [{'team': 'A', 'email': 'x'}]),
    }
    for name, (raw_text, events_rows) in variants.items():
        with tempfile.TemporaryDirectory() as tmp:
            base = write_case(Path(tmp) / 'in', catalog, checks, {'products': good_products})
            if raw_text is not None:
                (base / 'events.jsonl').write_text(raw_text)
            else:
                with (base / 'events.jsonl').open('w') as out:
                    for row in events_rows:
                        out.write(json.dumps(row) + '\n')
            result = engine.execute_plan(catalog, checks, base_dir=base, out_dir=Path(tmp) / 'out')
            check('corrupt/%s_incomplete' % name, result['status'] == 'incomplete'
                  and result['all_pass'] is None, json.dumps(result.get('error')))
            by_id = {c['check_id']: c for c in result['checks']}
            check('corrupt/%s_events_check_not_passed' % name,
                  by_id['nn']['status'] == 'not_evaluated' and by_id['nn']['passed'] is None,
                  str(by_id['nn']))
            check('corrupt/%s_other_check_still_evaluated' % name,
                  by_id['uq']['status'] == 'evaluated', str(by_id['uq']))
    with tempfile.TemporaryDirectory() as tmp:
        base = write_case(Path(tmp) / 'in', catalog, checks,
                          {'events': good_events, 'products': good_products})
        os.remove(base / 'events.jsonl')
        result = engine.execute_plan(catalog, checks, base_dir=base, out_dir=Path(tmp) / 'out')
        check('corrupt/missing_file_incomplete', result['status'] == 'incomplete',
              json.dumps(result.get('error')))


def test_scale_and_readonly():
    catalog = {'sources': {
        's1': {'file': 's1.jsonl', 'schema': {'row_id': 'text', 'team': 'text',
                                              'email': ['text', 'null'], 'code': 'int',
                                              'key': 'text'}},
        's2': {'file': 's2.jsonl', 'schema': {'row_id': 'text', 'ref': 'text', 'active': 'bool',
                                              'val': ['int', 'null'], 'x': 'text'}},
        's3': {'file': 's3.jsonl', 'schema': {'row_id': 'text', 'k1': 'text',
                                              'k2': ['text', 'null'], 'n': 'int'}}}}
    n = 3000
    s1 = [{'row_id': 'a%04d' % i, 'team': 'T%d' % (i % 5),
           'email': None if i % 13 == 0 else 'e%d' % i, 'code': i % 97,
           'key': 'k%d' % (i % 250)} for i in range(n)]
    s2 = [{'row_id': 'b%04d' % i, 'ref': 'k%d' % ((i * 7) % 260), 'active': i % 2 == 0,
           'val': None if i % 11 == 0 else i, 'x': 'x%d' % (i % 100)} for i in range(n)]
    s3 = [{'row_id': 'c%04d' % i, 'k1': 'y%d' % (i % 40),
           'k2': None if i % 5 == 0 else 'z%d' % i, 'n': i} for i in range(n)]
    checks = [
        {'check_id': 'c1', 'kind': 'not_null', 'source': 's1', 'field': 'email',
         'population': {'op': 'all'}},
        {'check_id': 'c2', 'kind': 'unique', 'source': 's1', 'fields': ['key'],
         'population': {'op': 'all'}, 'nulls': 'equal'},
        {'check_id': 'c3', 'kind': 'unique', 'source': 's1', 'fields': ['team', 'code'],
         'population': {'op': 'all'}, 'nulls': 'ignore'},
        {'check_id': 'c4', 'kind': 'reference', 'source': 's1',
         'population': {'op': 'all'}, 'target_source': 's2',
         'target_population': {'op': 'all'}, 'source_fields': ['key'],
         'target_fields': ['ref'], 'nulls': 'ignore'},
        {'check_id': 'c5', 'kind': 'reference', 'source': 's1',
         'population': {'op': 'all'}, 'target_source': 's2',
         'target_population': {'op': 'eq', 'field': 'active', 'value': True},
         'source_fields': ['key'], 'target_fields': ['ref'], 'nulls': 'reject'},
        {'check_id': 'c6', 'kind': 'reference', 'source': 's2',
         'population': {'op': 'all'}, 'target_source': 's1',
         'target_population': {'op': 'all'}, 'source_fields': ['ref'],
         'target_fields': ['key'], 'nulls': 'ignore'},
        {'check_id': 'c7', 'kind': 'not_null', 'source': 's2', 'field': 'val',
         'population': {'op': 'or', 'args': [{'op': 'eq', 'field': 'active', 'value': True},
                                             {'op': 'is_null', 'field': 'val'}]}},
        {'check_id': 'c8', 'kind': 'unique', 'source': 's2', 'fields': ['x'],
         'population': {'op': 'all'}, 'nulls': 'ignore'},
        {'check_id': 'c9', 'kind': 'reference', 'source': 's3',
         'population': {'op': 'all'}, 'target_source': 's1',
         'target_population': {'op': 'all'}, 'source_fields': ['k1'],
         'target_fields': ['key'], 'nulls': 'ignore'},
        {'check_id': 'c10', 'kind': 'unique', 'source': 's3', 'fields': ['k1', 'k2'],
         'population': {'op': 'all'}, 'nulls': 'equal'},
    ]

    with tempfile.TemporaryDirectory() as tmp:
        base = write_case(Path(tmp) / 'in', catalog, checks, {'s1': s1, 's2': s2, 's3': s3})
        hashes = {}
        for name, spec in catalog['sources'].items():
            path = Path(base) / spec['file']
            os.chmod(path, 0o444)  # read-only sources must not be rewritten
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        result = engine.execute_plan(catalog, checks, base_dir=base, out_dir=Path(tmp) / 'out')
        check('scale/status', result['status'] == 'complete', json.dumps(result.get('error')))
        check('scale/ten_checks', result['run']['checks_total'] == 10
              and result['run']['checks_evaluated'] == 10, '')
        expected = oracle(base, catalog, checks)
        by_id = {c['check_id']: c for c in result['checks']}
        ok = True
        for cid, exp in expected.items():
            got = by_id[cid]
            same = all(got[f] == exp[f] for f in
                       ('population_count', 'evaluated_count', 'violation_count', 'passed')) \
                and got['witnesses'] == exp['witnesses']
            if not same:
                ok = False
                print('  mismatch %s: got %s want %s' % (cid, json.dumps(got), json.dumps(exp)))
        check('scale/all_counts_and_witnesses_match_oracle', ok, '')
        for name in ('s1', 's2', 's3'):
            check('scale/%s_scanned_once' % name,
                  result['sources'][name]['scans'] == 1
                  and result['sources'][name]['decoded'] == n,
                  str(result['sources'][name]))
        check('scale/resident_peak_direct', result['resident']['peak_rows'] <= 64,
              json.dumps(result['resident']))
        for name in ('s1', 's2', 's3'):
            path = Path(base) / catalog['sources'][name]['file']
            check('scale/%s_unmodified' % name,
                  hashlib.sha256(path.read_bytes()).hexdigest() == hashes[name]
                  and not os.access(path, os.W_OK), '')
            os.chmod(path, 0o644)


def main():
    test_materials()
    test_probe_and_benchmark_scripts()
    test_cli_on_materials()
    test_cross_batch_duplicates()
    test_two_references_shared_target()
    test_target_duplicate_keys()
    test_all_ignored_and_empty()
    test_typed_values()
    test_benchmark_1000()
    test_config_errors()
    test_corrupted_sources()
    test_scale_and_readonly()
    print()
    if FAILED:
        print('%d FAILURES: %s' % (len(FAILED), FAILED))
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
