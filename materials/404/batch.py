"""Batch preview entry point that can target an explicit SQLite database."""

from __future__ import annotations

import json
import sys

from session import Session


def run(path, db_path=None, pumps=None):
    """Pump ``path`` and return ``{'db_path', 'snapshots'}``.

    ``pumps`` limits the number of envelopes consumed; ``None`` pumps until the
    session reaches a terminal state.  The returned snapshots are read through
    a fresh independent SQLite consumer, exactly like the analysis dashboard.
    """
    session = Session(path, db_path)
    snapshots = []
    if pumps is None:
        while True:
            snapshots.append(session.pump())
            if session.status in ('complete', 'complete_with_errors',
                                  'incomplete', 'failed'):
                break
    else:
        for _ in range(pumps):
            snapshots.append(session.pump())
    return {'db_path': session.db_path, 'status': session.status,
            'snapshots': snapshots, 'session_error': session.session_error}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print('usage: batch.py <fixture.json> [db_path] [pumps]')
        return 2
    path = argv[0]
    db_path = argv[1] if len(argv) > 1 and argv[1] not in ('-', '') else None
    pumps = int(argv[2]) if len(argv) > 2 else None
    result = run(path, db_path, pumps)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
