"""Independent SQL consumer. Its transaction is the public preview boundary."""
import json
import sqlite3


def snapshot(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        meta = db.execute('SELECT status,last_frame,errors_json FROM preview_meta WHERE singleton=1').fetchone()
        orders = [dict(r) for r in db.execute('SELECT * FROM orders ORDER BY id')]
        lines = [dict(r) for r in db.execute('SELECT * FROM lines ORDER BY order_id,id')]
        return {'status': meta['status'], 'last_frame': meta['last_frame'],
                'errors': json.loads(meta['errors_json']), 'orders': orders, 'lines': lines}
