import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent


def invoke(arguments):
    command = [sys.executable, '-B', *map(str, arguments)]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=20)
    return {'command': command, 'cwd': str(root), 'exit_code': result.returncode,
            'stdout': result.stdout, 'stderr': result.stderr}


with tempfile.TemporaryDirectory() as directory:
    report = {'scope': 'Actual page/WAL/process baseline and a fixed two-delta undo feasibility check; not a target recovery implementation.', 'cases': []}
    roots = {}
    for name in ('normal', 'winner-loser', 'unsafe-flush', 'abort-unflushed', 'abort-reuse'):
        working = Path(directory) / name
        shutil.copytree(root / 'seed', working)
        roots[name] = working
        before = invoke(['main.py', 'script', working, root / 'fixtures' / (name + '.json')])
        assert before['exit_code'] == 73, before
        disk_before = json.loads((working / 'pages/p0.json').read_text())
        durable_before = [json.loads(line) for line in (working / 'wal.jsonl').read_text().splitlines()]
        after = invoke(['main.py', 'recover', working])
        assert after['exit_code'] == 0, after
        disk_after = json.loads((working / 'pages/p0.json').read_text())
        report['cases'].append({'name': name, 'writer': before, 'disk_at_crash': disk_before,
                                'durable_wal_at_crash': durable_before, 'baseline_recover': after,
                                'disk_after_baseline': disk_after,
                                'expected_rows': {'alpha': 15 if name == 'normal' else 17 if name == 'abort-reuse' else 10,
                                                  'beta': 22 if name in ('winner-loser', 'abort-unflushed') else 20,
                                                  'untouched': 731}})
    working = roots['winner-loser']
    gap_root = Path(directory) / 'log-gap'
    shutil.copytree(working, gap_root)
    gap_stop = invoke(['diagnostic_undo.py', gap_root, 'crash-after-log'])
    assert gap_stop['exit_code'] == 76
    gap_page = json.loads((gap_root / 'pages/p0.json').read_text())
    gap_wal_last = json.loads((gap_root / 'wal.jsonl').read_text().splitlines()[-1])
    assert gap_wal_last['lsn'] > gap_page['page_lsn']
    gap_resume = invoke(['diagnostic_undo.py', gap_root])
    assert gap_resume['exit_code'] == 0
    assert json.loads((gap_root / 'pages/p0.json').read_text())['rows'] == {'alpha': 10, 'beta': 22, 'untouched': 731}
    interruption = invoke(['diagnostic_undo.py', working, 'crash-after-first-point-undo'])
    assert interruption['exit_code'] == 75
    persisted_midway = json.loads((working / 'pages/p0.json').read_text())
    resume = invoke(['diagnostic_undo.py', working])
    assert resume['exit_code'] == 0
    final = json.loads((working / 'pages/p0.json').read_text())
    assert persisted_midway['rows'] == {'alpha': 99, 'beta': 22, 'untouched': 731}
    assert final['rows'] == {'alpha': 10, 'beta': 22, 'untouched': 731}
    again = invoke(['diagnostic_undo.py', working])
    assert again['exit_code'] == 0
    assert json.loads((working / 'pages/p0.json').read_text()) == final
    report['fixed_undo_feasibility'] = {'interruption': interruption, 'actual_persisted_midpoint': persisted_midway,
                                        'resume': resume, 'actual_final_page': final, 'repeat': again,
                                        'scope': 'Only replays the fixed L/alpha two ADD records using logged inverse deltas and page_lsn; it deliberately does not publish READY or handle later business writes.'}
    report['fixed_wal_page_gap_feasibility'] = {'stop': gap_stop, 'persisted_page_at_stop': gap_page,
                                               'actual_last_durable_record': gap_wal_last, 'resume': gap_resume}
    print(json.dumps(report))
