"""End-to-end probe against real page files and WAL across process exits.

Runs the five fixture schedules in child processes killed with os._exit(73),
reopens every work directory in fresh recovery processes, and checks the real
page files.  Also verifies:

* every engine page write was preceded by a durable WAL prefix (the old
  unsafe-flush state can no longer be produced),
* recovery is idempotent (running it again changes nothing),
* a hand-crafted old-style directory with page_lsn beyond the durable WAL end
  is rejected with a non-zero exit instead of a guessed value,
* after recovery a new transaction can reuse a previously aborted row.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent
expected_rows = {
    'normal': {'alpha': 15, 'beta': 20, 'untouched': 731},
    'winner-loser': {'alpha': 10, 'beta': 22, 'untouched': 731},
    # The engine now forces the ADD before writing the page, so this is no
    # longer an unrecoverable gap: recovery finds loser L and undoes it.
    'unsafe-flush': {'alpha': 10, 'beta': 20, 'untouched': 731},
    'abort-unflushed': {'alpha': 10, 'beta': 22, 'untouched': 731},
    'abort-reuse': {'alpha': 17, 'beta': 20, 'untouched': 731},
}


def invoke(arguments):
    command = [sys.executable, '-B', *map(str, arguments)]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=20)
    return {'command': command, 'cwd': str(root), 'exit_code': result.returncode,
            'stdout': result.stdout, 'stderr': result.stderr}


def rows_of(working, page='p0'):
    return json.loads((working / 'pages' / (page + '.json')).read_text())['rows']


with tempfile.TemporaryDirectory() as directory:
    report = {'scope': 'Real page/WAL/process recovery probe for the target design.',
              'cases': [], 'fail_closed_gap': None, 'reuse_after_recovery': None}
    roots = {}
    crash_state = {}
    for name in ('normal', 'winner-loser', 'unsafe-flush', 'abort-unflushed', 'abort-reuse'):
        working = Path(directory) / name
        shutil.copytree(root / 'seed', working)
        roots[name] = working
        before = invoke(['main.py', 'script', working, root / 'fixtures' / (name + '.json')])
        assert before['exit_code'] == 73, before
        writer = json.loads(before['stdout'])
        # WAL-before-page: every observed page write had a covering durable prefix.
        for event in writer['engine']:
            if 'durable_lsn_before_page_write' not in event:
                continue
            assert event['durable_lsn_before_page_write'] >= event['page_lsn'], event
        # Snapshot the exact crash state: the fixed diagnostic still requires
        # the pre-recovery two-ADD L/alpha layout.
        snapshot = Path(directory) / (name + '-crash-state')
        shutil.copytree(working, snapshot)
        crash_state[name] = snapshot
        disk_before = json.loads((working / 'pages/p0.json').read_text())
        durable_before = [json.loads(line)
                          for line in (working / 'wal.jsonl').read_text().splitlines()]
        after = invoke(['main.py', 'recover', working])
        assert after['exit_code'] == 0, after
        disk_after = json.loads((working / 'pages/p0.json').read_text())
        assert rows_of(working) == expected_rows[name], (name, rows_of(working))
        # READY must be repeatable: a second recovery changes no bytes.
        again = invoke(['main.py', 'recover', working])
        assert again['exit_code'] == 0, again
        disk_again = json.loads((working / 'pages/p0.json').read_text())
        assert disk_again == disk_after, (name, disk_again, disk_after)
        report['cases'].append({'name': name, 'writer': before,
                                'disk_at_crash': disk_before,
                                'durable_wal_at_crash': durable_before,
                                'recover': after, 'disk_after_recover': disk_after,
                                'repeat_recover': again,
                                'expected_rows': expected_rows[name]})

    # Old-style unsafe directory: page claims page_lsn=2 while the durable WAL
    # is empty.  Recovery must fail closed rather than invent a value.
    gap_root = Path(directory) / 'handcrafted-gap'
    shutil.copytree(root / 'seed', gap_root)
    page_path = gap_root / 'pages/p0.json'
    gap_page = json.loads(page_path.read_text())
    gap_page['page_lsn'] = 2
    gap_page['rows']['alpha'] = 99
    page_path.write_text(json.dumps(gap_page, sort_keys=True))
    (gap_root / 'wal.jsonl').write_text('')
    gap = invoke(['main.py', 'recover', gap_root])
    assert gap['exit_code'] == 2, gap
    assert 'beyond the durable WAL end' in gap['stdout'], gap
    inspect_gap = invoke(['main.py', 'inspect', gap_root])
    assert inspect_gap['exit_code'] == 2, inspect_gap
    # The unrepairable directory must be left untouched.
    assert json.loads(page_path.read_text()) == gap_page
    report['fail_closed_gap'] = {'recover': gap, 'inspect': inspect_gap,
                                 'page_left_untouched': json.loads(page_path.read_text())}

    # Recovery then live business: reuse a row that belonged to a rolled-back
    # transaction, commit, crash without flush, and recover the new increment.
    working = roots['winner-loser']
    reuse_schedule = Path(directory) / 'reuse.json'
    reuse_schedule.write_text(json.dumps([
        {'op': 'begin', 'tx': 'T2'},
        {'op': 'add', 'tx': 'T2', 'page': 'p0', 'key': 'alpha', 'delta': 7},
        {'op': 'commit', 'tx': 'T2'},
        {'op': 'crash'},
    ]))
    reuse_run = invoke(['main.py', 'script', working, reuse_schedule])
    assert reuse_run['exit_code'] == 73, reuse_run
    reuse_recover = invoke(['main.py', 'recover', working])
    assert reuse_recover['exit_code'] == 0, reuse_recover
    assert rows_of(working) == {'alpha': 17, 'beta': 22, 'untouched': 731}
    # A still older loser (L) must not be undone a second time.
    final_wal = [json.loads(line)
                 for line in (working / 'wal.jsonl').read_text().splitlines()]
    undo_targets = [record['undo_of'] for record in final_wal if record['kind'] == 'UNDO']
    assert sorted(undo_targets) == sorted(set(undo_targets)), undo_targets
    report['reuse_after_recovery'] = {'script': reuse_run,
                                      'recover': reuse_recover,
                                      'rows': rows_of(working)}

    # The fixed diagnostic stays as the narrow two-ADD feasibility demonstration.
    # It runs on pristine crash-state copies (generic recovery never touched them).
    pristine = crash_state['winner-loser']
    diag_root = Path(directory) / 'log-gap'
    shutil.copytree(pristine, diag_root)
    gap_stop = invoke(['diagnostic_undo.py', diag_root, 'crash-after-log'])
    assert gap_stop['exit_code'] == 76
    gap_page = json.loads((diag_root / 'pages/p0.json').read_text())
    gap_wal_last = json.loads((diag_root / 'wal.jsonl').read_text().splitlines()[-1])
    assert gap_wal_last['lsn'] > gap_page['page_lsn']
    gap_resume = invoke(['diagnostic_undo.py', diag_root])
    assert gap_resume['exit_code'] == 0
    assert rows_of(diag_root) == {'alpha': 10, 'beta': 22, 'untouched': 731}
    interruption_root = Path(directory) / 'midpoint'
    shutil.copytree(pristine, interruption_root)
    interruption = invoke(['diagnostic_undo.py', interruption_root,
                           'crash-after-first-point-undo'])
    assert interruption['exit_code'] == 75
    persisted_midway = json.loads((interruption_root / 'pages/p0.json').read_text())
    resume = invoke(['diagnostic_undo.py', interruption_root])
    assert resume['exit_code'] == 0
    final = json.loads((interruption_root / 'pages/p0.json').read_text())
    assert persisted_midway['rows'] == {'alpha': 99, 'beta': 22, 'untouched': 731}
    assert final['rows'] == {'alpha': 10, 'beta': 22, 'untouched': 731}
    again = invoke(['diagnostic_undo.py', interruption_root])
    assert again['exit_code'] == 0
    assert json.loads((interruption_root / 'pages/p0.json').read_text()) == final
    report['fixed_undo_feasibility'] = {
        'interruption': interruption, 'actual_persisted_midpoint': persisted_midway,
        'resume': resume, 'actual_final_page': final, 'repeat': again,
        'scope': 'Only replays the fixed L/alpha two ADD records using logged '
                 'inverse deltas and page_lsn; deliberately not a general recovery engine.'}
    report['fixed_wal_page_gap_feasibility'] = {
        'stop': gap_stop, 'persisted_page_at_stop': gap_page,
        'actual_last_durable_record': gap_wal_last, 'resume': gap_resume}
    print(json.dumps(report))
