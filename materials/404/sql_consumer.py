"""Independent SQL consumer. Its transaction is the public preview boundary."""

from __future__ import annotations

import json
import sqlite3


def snapshot(path):
    """Return one whole published preview using one read transaction.

    A concurrent writer either committed a complete frame or nothing, so the
    three tables always agree.  WAL mode additionally lets this reader keep
    seeing its old snapshot while a new frame is being prepared.
    """
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        meta = db.execute(
            'SELECT status,last_frame,errors_json FROM preview_meta '
            'WHERE singleton=1').fetchone()
        if meta is None:
            db.execute('ROLLBACK')
            return {'status': 'new', 'last_frame': 0, 'errors': [],
                    'orders': [], 'lines': []}
        orders = [dict(r) for r in
                  db.execute('SELECT * FROM orders ORDER BY id')]
        lines = [dict(r) for r in
                 db.execute('SELECT * FROM lines ORDER BY order_id,id')]
        db.execute('COMMIT')
        return {'status': meta['status'], 'last_frame': meta['last_frame'],
                'errors': json.loads(meta['errors_json']),
                'orders': orders, 'lines': lines}
    except BaseException:
        db.execute('ROLLBACK')
        raise
    finally:
        db.close()
