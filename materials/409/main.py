"""Controlled entries:

    python main.py script  ROOT SCHEDULE   run begin/add/commit/abort/flush/crash
    python main.py recover ROOT            restart recovery, print READY report
    python main.py inspect ROOT            read-only durable-state inspection

Recovery fault points (used by tests to stop on a real persistence boundary):

    --fault after-undo-log   after an UNDO was appended+forced, before its page write
    --fault after-abort      after an ABORT decision was forced, before redo
    --fault after-redo-write after a redo page write was fsynced/replaced
    --fault before-ready     after all recovery IO, immediately before READY
    --crash-after N          legacy integer form: 0 = after-undo-log,
                             N > 0 = after the Nth recovery page write
"""
import argparse
import json
import os
import sys

from engine import Engine
from recovery import (EXIT_ABORT_LOG, EXIT_BEFORE_READY, EXIT_UNDO_LOG,
                      EXIT_UNDO_PAGE, RecoveryError, inspect, recover)

parser = argparse.ArgumentParser()
commands = parser.add_subparsers(dest='command', required=True)
script = commands.add_parser('script')
script.add_argument('root')
script.add_argument('schedule')
restart = commands.add_parser('recover')
restart.add_argument('root')
restart.add_argument('--crash-after', type=int)
restart.add_argument('--fault', choices=('after-undo-log', 'after-abort',
                                         'after-redo-write', 'before-ready'))
inspection = commands.add_parser('inspect')
inspection.add_argument('root')
args = parser.parse_args()

if args.command == 'inspect':
    try:
        print(json.dumps(inspect(args.root), sort_keys=True))
    except RecoveryError as error:
        print(json.dumps({'status': 'invalid_directory', 'error': str(error)}))
        sys.exit(2)
elif args.command == 'recover':
    try:
        report = recover(args.root, crash_after=args.crash_after, fault=args.fault)
    except RecoveryError as error:
        print(json.dumps({'status': 'invalid_directory', 'error': str(error)}))
        sys.exit(2)
    print(json.dumps(report, sort_keys=True))
else:
    try:
        # Recovery runs first inside the engine so business entries are only
        # open once historical transactions have been resolved.
        engine = Engine(args.root)
    except RecoveryError as error:
        print(json.dumps({'status': 'invalid_directory', 'error': str(error)}))
        sys.exit(2)
    for event in json.load(open(args.schedule)):
        if event['op'] == 'crash':
            print(json.dumps({'engine': engine.audit, 'wal': engine.wal.audit,
                              'page_io': engine.pages.audit,
                              'actual_durable_lsn': engine.wal.durable_lsn()}), flush=True)
            os._exit(73)
        try:
            if event['op'] in ('begin', 'commit', 'abort'):
                getattr(engine, event['op'])(event['tx'])
            elif event['op'] == 'add':
                engine.add(event['tx'], event['page'], event['key'], event['delta'])
            elif event['op'] == 'flush':
                engine.flush(event['page'])
            else:
                raise ValueError('unknown schedule operation')
        except (ValueError, OverflowError) as error:
            # Controlled rejection (LOCKED / int64 overflow / bad delta): the
            # schedule marks such events expected-error; anything else aborts.
            if not event.get('expected_error'):
                raise
            if event['expected_error'] not in str(error):
                raise
    print(json.dumps({'engine': engine.audit, 'wal': engine.wal.audit,
                      'page_io': engine.pages.audit,
                      'recovery': engine.recovery_report}))
