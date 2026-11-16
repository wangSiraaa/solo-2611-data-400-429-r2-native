"""Atomic SQLite publication of one whole preview frame.

The historical implementation committed ``orders``, ``lines`` and
``preview_meta`` separately, so an independent reader could mix two frames or
observe a frame whose publication later failed.  Every write here happens in a
single ``BEGIN IMMEDIATE`` transaction.  WAL mode keeps an already-open reader
on its old snapshot while the writer prepares the next one.
"""

from __future__ import annotations

import json
import sqlite3

from incremental import path_text

SCHEMA = '''
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
        last_changed_frame INTEGER NOT NULL,
        PRIMARY KEY(order_id,id));
    INSERT OR IGNORE INTO preview_meta VALUES(1,'new',0,'[]');
'''


def _source_path(row):
    """Accept the session's tuple form and the older string form."""
    if 'path' in row:
        return path_text(row['path'])
    return row.get('source_path', '')


class SQLiteView:
    def __init__(self, path):
        self.path = str(path)
        db = self._connect()
        try:
            # Persisted in the database file; do it only once so it never has
            # to fight an already-open independent read transaction.
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
        finally:
            db.close()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA synchronous=NORMAL')
        return db

    def reset(self):
        """Start a session from the empty frame-0 preview."""
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('DELETE FROM orders')
                db.execute('DELETE FROM lines')
                db.execute(
                    "UPDATE preview_meta SET status='new',last_frame=0,"
                    "errors_json='[]' WHERE singleton=1")
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        finally:
            db.close()

    def _write_frame(self, db, preview):
        rows = []
        for row in preview['orders']:
            rows.append((row['id'], row.get('tax'), _source_path(row),
                         row.get('created_frame', 0),
                         row.get('last_changed_frame', 0)))
        db.execute('DELETE FROM orders')
        db.executemany('INSERT INTO orders VALUES(?,?,?,?,?)', rows)

        rows = []
        for row in preview['lines']:
            rows.append((row['order_id'], row['id'], row.get('qty'),
                         _source_path(row), row.get('created_frame', 0),
                         row.get('last_changed_frame', 0)))
        db.execute('DELETE FROM lines')
        db.executemany('INSERT INTO lines VALUES(?,?,?,?,?,?)', rows)

        db.execute(
            'UPDATE preview_meta SET status=?,last_frame=?,errors_json=? '
            'WHERE singleton=1',
            (preview['status'], preview['last_frame'],
             json.dumps(preview.get('errors', []), ensure_ascii=False)))

    def publish(self, preview, before_commit=None):
        """Publish rows and metadata atomically, or leave the old frame intact.

        ``before_commit`` runs inside the still-uncommitted transaction.  It
        may raise (for example ``OSError``) to roll the whole frame back.
        """
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._write_frame(db, preview)
                if before_commit is not None:
                    before_commit()
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        finally:
            db.close()

    def set_status(self, status, errors, last_frame):
        """Change only the terminal/status metadata; rows stay untouched."""
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute(
                    'UPDATE preview_meta SET status=?,last_frame=?,'
                    'errors_json=? WHERE singleton=1',
                    (status, last_frame,
                     json.dumps(errors, ensure_ascii=False)))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        finally:
            db.close()

    def query(self):
        from sql_consumer import snapshot
        return snapshot(self.path)
