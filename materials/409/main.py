import argparse
import json
import os
from engine import Engine
from recovery import recover

parser = argparse.ArgumentParser()
commands = parser.add_subparsers(dest='command', required=True)
script = commands.add_parser('script')
script.add_argument('root')
script.add_argument('schedule')
restart = commands.add_parser('recover')
restart.add_argument('root')
restart.add_argument('--crash-after', type=int)
args = parser.parse_args()
if args.command == 'recover':
    print(json.dumps(recover(args.root, args.crash_after)))
else:
    engine = Engine(args.root)
    for event in json.load(open(args.schedule)):
        if event['op'] == 'crash':
            print(json.dumps({'engine': engine.audit, 'wal': engine.wal.audit,
                              'actual_durable_lsn': engine.wal.durable_lsn()}), flush=True)
            os._exit(73)
        if event['op'] in ('begin', 'commit', 'abort'):
            getattr(engine, event['op'])(event['tx'])
        elif event['op'] == 'add':
            engine.add(event['tx'], event['page'], event['key'], event['delta'])
        elif event['op'] == 'flush':
            engine.flush(event['page'])
        else:
            raise ValueError('unknown schedule operation')
    print(json.dumps({'engine': engine.audit, 'wal': engine.wal.audit}))
