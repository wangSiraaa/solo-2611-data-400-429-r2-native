"""Report the traversal/write cost of each delivered frame.

Run against the bundled fixtures (all amounts stay within the exercise
bounds).  ``nodes`` counts relation nodes visited while materializing the
JSON tree, ``minimal_writes`` counts rows added/changed/removed, and
``sql_rows_written`` is what the full-snapshot publisher rewrites.  Comparing
the last two shows why a naive full rebuild is correct but not free, and how
much an ancestor null actually invalidates.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from session import Session


def analyse(path, db_path, max_pumps=16):
    session = Session(path, db_path)
    out = []
    for _ in range(max_pumps):
        preview = session.pump()
        stats = session.last_stats
        out.append({
            'frame': preview['last_frame'],
            'status': preview['status'],
            'nodes': stats.get('nodes', 0),
            'orders': len(preview['orders']),
            'lines': len(preview['lines']),
            'rows_added': stats.get('rows_added', 0),
            'rows_changed': stats.get('rows_changed', 0),
            'rows_removed': stats.get('rows_removed', 0),
            'rows_unchanged': stats.get('rows_unchanged', 0),
            'minimal_writes': stats.get('minimal_writes', 0),
            'sql_rows_written': len(preview['orders']) + len(preview['lines']),
        })
        if session.status in ('complete', 'complete_with_errors',
                              'incomplete', 'failed'):
            break
    return out


def main(argv):
    path = argv[0] if argv else 'fixtures/null-bubble.json'
    with tempfile.TemporaryDirectory() as directory:
        rows = analyse(path, Path(directory) / 'preview.sqlite')
    print(json.dumps({'fixture': path, 'frames': rows}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
