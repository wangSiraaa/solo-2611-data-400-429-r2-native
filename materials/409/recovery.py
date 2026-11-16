"""Original redo-only recovery handles committed-only ordinary work."""
import os
from pathlib import Path
from page_io import Pages
from wal import Journal


def recover(root, crash_after=None):
    wal = Journal(root)
    pages = Pages(Path(root) / 'pages')
    records = wal.durable()
    winners = {entry['tx'] for entry in records if entry['kind'] == 'COMMIT'}
    writes = 0
    for entry in records:
        if entry['kind'] == 'ADD' and entry['tx'] in winners:
            page = pages.read(entry['page'])
            if entry['lsn'] <= page['page_lsn']:
                continue
            page['rows'][entry['key']] += entry['delta']
            page['page_lsn'] = entry['lsn']
            pages.write(entry['page'], page)
            writes += 1
            if crash_after == writes:
                os._exit(74)
    return {'status': 'ready', 'winner_transactions': sorted(winners),
            'actual_page_writes': writes, 'durable_lsn': wal.durable_lsn()}
