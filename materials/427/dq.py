#!/usr/bin/env python3
"""compile / run / inspect for the shared data-quality executor.

  python3 dq.py compile --input DIR   # validate config, print the shared plan
  python3 dq.py run     --input DIR   # execute the plan, write DIR/out/result.json
  python3 dq.py inspect --input DIR   # show results and source.py read counters
"""
import argparse
import json
import sys
from pathlib import Path

import compiler
import engine


def _load_json(path, what):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise compiler.ConfigError('%s not found: %s' % (what, path))
    except json.JSONDecodeError as error:
        raise compiler.ConfigError('%s is not valid JSON: %s' % (what, error))


def _load_config(input_dir):
    catalog = _load_json(Path(input_dir) / 'catalog.json', 'catalog')
    checks = _load_json(Path(input_dir) / 'checks.json', 'checks')
    return catalog, checks


def _out_dir(args):
    return Path(args.out) if args.out else Path(args.input) / 'out'


def cmd_compile(args):
    try:
        catalog, checks = _load_config(args.input)
        plan = compiler.compile_plan(catalog, checks)
    except compiler.ConfigError as error:
        print(json.dumps({'status': 'error', 'phase': 'compile', 'message': str(error)}, indent=2))
        return 2
    out_dir = _out_dir(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'plan.json').write_text(json.dumps(plan, indent=2, ensure_ascii=False))
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


def cmd_run(args):
    out_dir = _out_dir(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        catalog, checks = _load_config(args.input)
    except compiler.ConfigError as error:
        result = engine.incomplete_result('compile', str(error))
    else:
        result = engine.execute_plan(catalog, checks, base_dir=args.input, out_dir=out_dir,
                                     chunk_size=args.chunk_size)
    (out_dir / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
    summary = {'status': result['status'], 'all_pass': result['all_pass'],
               'error': result['error'],
               'checks': [{'check_id': c['check_id'], 'status': c['status'],
                           'population_count': c['population_count'],
                           'evaluated_count': c['evaluated_count'],
                           'violation_count': c['violation_count'],
                           'passed': c['passed']} for c in result['checks']],
               'sources': result['sources'],
               'staging': {'writes': result['staging']['writes'], 'reads': result['staging']['reads']},
               'resident': result['resident'],
               'result_file': str(out_dir / 'result.json')}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if result['status'] == 'complete' else 1


def cmd_inspect(args):
    path = _out_dir(args) / 'result.json'
    if not path.exists():
        print(json.dumps({'status': 'error', 'message': 'no run result at %s; run first' % path}))
        return 2
    result = json.loads(path.read_text())
    lines = ['run status: %s  all_pass: %s' % (result['status'], result['all_pass'])]
    if result.get('error'):
        lines.append('error: %s' % json.dumps(result['error'], ensure_ascii=False))
    plan = result.get('plan')
    if plan:
        lines.append('shared scans (actual plan):')
        for scan in plan['scans']:
            lines.append('  %s -> %s  read_columns=[%s]  consumers=%d'
                         % (scan['scan_id'], scan['file'], ','.join(scan['read_columns']),
                            len(scan['consumers'])))
    lines.append('checks:')
    for check in result['checks']:
        if check['status'] == 'evaluated':
            lines.append('  %s [%s] population=%d evaluated=%d violations=%d passed=%s'
                         % (check['check_id'], check['kind'], check['population_count'],
                            check['evaluated_count'], check['violation_count'], check['passed']))
            for witness in check['witnesses']:
                lines.append('    witness: %s' % json.dumps(witness, ensure_ascii=False, sort_keys=True))
        else:
            lines.append('  %s [%s] NOT EVALUATED (not counted as passed)' % (check['check_id'], check['kind']))
    lines.append('source.py real read counters:')
    for name, counter in result['sources'].items():
        lines.append('  %s (%s): scans=%d decoded=%d scan_complete=%s'
                     % (name, counter['file'], counter['scans'], counter['decoded'],
                        counter['scan_complete']))
    staging = result['staging']
    lines.append('staging (separate from source decodes): writes=%d reads=%d by_table=%s'
                 % (staging['writes'], staging['reads'], json.dumps(staging['by_table'])))
    resident = result['resident']
    lines.append('resident rows: peak=%d/%d components=%s'
                 % (resident['peak_rows'], resident['limit_rows'],
                    json.dumps(resident['peak_components'])))
    sqlite = result['sqlite']
    lines.append('sqlite: page_cache=%d KiB (%d bytes, disclosed separately from the row budget) '
                 'temp_store=%s state_file_bytes=%d'
                 % (sqlite['page_cache_kib'], sqlite['page_cache_bytes'],
                    sqlite['temp_store'], sqlite['state_file_bytes']))
    print('\n'.join(lines))
    return 0 if result['status'] == 'complete' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('compile', 'run', 'inspect'):
        subparser = sub.add_parser(name)
        subparser.add_argument('--input', required=True, help='directory with catalog.json/checks.json')
        subparser.add_argument('--out', default=None, help='output directory (default: INPUT/out)')
    sub.choices['run'].add_argument('--chunk-size', type=int, default=engine.CHUNK_SIZE,
                                    help='rows decoded per read batch (default: %(default)s)')
    args = parser.parse_args()
    handler = {'compile': cmd_compile, 'run': cmd_run, 'inspect': cmd_inspect}[args.command]
    return handler(args)


if __name__ == '__main__':
    sys.exit(main())
