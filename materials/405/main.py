import argparse
import json
from run_store import Store
from reader import get, scan
from compactor import compact
from inspect import inspect

p = argparse.ArgumentParser()
p.add_argument('root')
s = p.add_subparsers(dest='cmd', required=True)
a = s.add_parser('add')
a.add_argument('id')
a.add_argument('records')
a = s.add_parser('get')
a.add_argument('key', type=int)
a = s.add_parser('scan')
a.add_argument('lo', type=int)
a.add_argument('hi', type=int)
a = s.add_parser('compact')
a.add_argument('out')
a.add_argument('selected', nargs='+')
s.add_parser('inspect')
a = p.parse_args()

store = Store(a.root)
if a.cmd == 'add':
    out = store.add(a.id, json.load(open(a.records)))
elif a.cmd == 'get':
    out = get(store, a.key)
elif a.cmd == 'scan':
    out = scan(store, a.lo, a.hi)
elif a.cmd == 'compact':
    out = compact(store, a.selected, a.out)
else:
    out = inspect(store)
print(json.dumps(out, ensure_ascii=False))
