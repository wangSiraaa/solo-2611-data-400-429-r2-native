"""Existing SQL preview publisher; each table currently commits separately.

This is editable application code, not a protected facility. The independent
reader in sql_consumer.py models a dashboard using its own SQLite connection.
"""
import json
import sqlite3


class SQLiteView:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS preview_meta(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    status TEXT NOT NULL, last_frame INTEGER NOT NULL,
                    errors_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders(
                    id TEXT PRIMARY KEY, tax INTEGER, source_path TEXT NOT NULL,
                    created_frame INTEGER NOT NULL, last_changed_frame INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS lines(
                    order_id TEXT NOT NULL, id TEXT NOT NULL, qty INTEGER,
                    source_path TEXT NOT NULL, created_frame INTEGER NOT NULL,
                    last_changed_frame INTEGER NOT NULL, PRIMARY KEY(order_id,id));
                INSERT OR IGNORE INTO preview_meta VALUES(1,'new',0,'[]');
            ''')

    def publish(self, preview, before_commit=None):
        # An external reader can observe rows from a new frame and old metadata.
        # A late failure likewise leaves a partially published frame behind.
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM orders')
            db.executemany('INSERT INTO orders VALUES(?,?,?,?,?)', [
                (r['id'], r.get('tax'), r.get('source_path', ''),
                 r.get('created_frame', 1), r.get('last_changed_frame', 1))
                for r in preview['orders']])
            db.commit()
            db.execute('DELETE FROM lines')
            db.executemany('INSERT INTO lines VALUES(?,?,?,?,?,?)', [
                (r['order_id'], r['id'], r.get('qty'), r.get('source_path', ''),
                 r.get('created_frame', 1), r.get('last_changed_frame', 1))
                for r in preview['lines']])
            db.commit()
            if before_commit is not None:
                before_commit()
            db.execute('UPDATE preview_meta SET status=?,last_frame=?,errors_json=? WHERE singleton=1',
                       (preview['status'], preview['last_frame'],
                        json.dumps(preview.get('errors', []), ensure_ascii=False)))

    def query(self):
        from sql_consumer import snapshot
        return snapshot(self.path)
