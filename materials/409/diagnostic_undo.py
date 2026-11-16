"""Only the fixed L/alpha fixture: demonstrate idempotent point undo across a real exit.

This is not a general recovery engine: no transaction completion publication,
live abort, writer coordination, or general transaction analysis is implemented here.
"""
import json
import os
import sys
from pathlib import Path
from page_io import Pages
from wal import Journal

root = Path(sys.argv[1])
pages = Pages(root / 'pages')
journal = Journal(root)
records = [entry for entry in journal.durable()
           if entry['kind'] == 'ADD' and entry['tx'] == 'L' and entry['key'] == 'alpha']
assert len(records) == 2
already = {entry['undo_of'] for entry in journal.durable() if entry['kind'] == 'UNDO'}
for entry in journal.durable():
    if entry['kind'] == 'UNDO' and entry['tx'] == 'L' and entry['key'] == 'alpha':
        page = pages.read('p0')
        if entry['lsn'] > page['page_lsn']:
            page['rows']['alpha'] += entry['delta']
            page['page_lsn'] = entry['lsn']
            pages.write('p0', page)
for index, entry in enumerate(reversed(records)):
    if entry['lsn'] in already:
        continue
    page = pages.read('p0')
    compensation = journal.append({'kind': 'UNDO', 'tx': 'L', 'prev_lsn': 0,
                                   'page': 'p0', 'key': 'alpha', 'delta': -entry['delta'],
                                   'undo_of': entry['lsn'], 'undo_next': entry['prev_lsn']})
    journal.force(compensation)
    if len(sys.argv) > 2 and sys.argv[2] == 'crash-after-log':
        print(json.dumps({'forced_compensation': compensation, 'page_before': page}), flush=True)
        os._exit(76)
    page['rows']['alpha'] -= entry['delta']
    page['page_lsn'] = compensation
    pages.write('p0', page)
    print(json.dumps({'fixed_undo_lsn': entry['lsn'], 'persisted_page': pages.read('p0')}), flush=True)
    if len(sys.argv) > 2 and index == 0:
        os._exit(75)
