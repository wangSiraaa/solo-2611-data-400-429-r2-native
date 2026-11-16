"""Persistent compound multikey catalog (SQLite backed).

Operations: ``build`` / open / ``put`` / ``remove`` / ``query`` / ``explain``
/ ``verify``, all against a local directory that is consistent after reopen.

Write atomicity: every ``put``/``remove`` runs in one SQLite transaction that
replaces the document payload and *all* of its index postings together.  An
error rolls back, so a document and its keys always commit or disappear as a
unit (local single-file storage only - distributed transactions are out of
scope).

Execution accounting is measured from the real statements that run; the
explain output is assembled from those counters afterwards, never computed
from a precomputed answer.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from evaluator import evaluate
from indexer import (
    IndexSpec,
    TAG_EMPTY,
    TAG_MISSING,
    TAG_NULL,
    extract_keys,
    validate_document,
)
from planner import collect_scans, plan_query

_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')
_DB_FILENAME = 'catalog.sqlite'

# Tags whose slot carries no payload.  WITHOUT ROWID primary keys require
# every column NOT NULL, so the payload column stores an explicit empty-text
# sentinel for these tags.  The tag itself keeps missing / empty-array /
# explicit null fully distinct - they never collapse into one value.
_PLACEHOLDER_TAGS = frozenset((TAG_MISSING, TAG_EMPTY, TAG_NULL))
_PLACEHOLDER_VALUE = ''


def _to_storage(tag, value):
    if value is None:
        if tag not in _PLACEHOLDER_TAGS:
            raise AssertionError('unexpected NULL payload for tag %r' % tag)
        return _PLACEHOLDER_VALUE
    return value


def _from_storage(tag, value):
    return None if tag in _PLACEHOLDER_TAGS else value


def _table_name(index_name):
    if not _NAME_RE.match(index_name):
        raise ValueError('illegal index name: %r' % index_name)
    return 'idx_' + index_name


class Catalog:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / _DB_FILENAME
        if not self.path.exists():
            raise FileNotFoundError('no catalog in %r; call Catalog.build first' % str(self.directory))
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self._specs = self._load_specs()
        self.last_explain = None

    # ------------------------------------------------------------------ setup
    @classmethod
    def build(cls, directory, indexes, documents=(), *, force=False):
        """Create a fresh catalog.

        ``indexes`` is either a list of path lists (named idx0, idx1, ...) or
        a mapping ``name -> paths``.  Cross-array indexes are rejected here,
        before any storage is touched.
        """
        directory = Path(directory)
        db_path = directory / _DB_FILENAME
        if db_path.exists() and not force:
            raise FileExistsError('catalog already exists at %r' % str(directory))

        named_specs = _normalize_indexes(indexes)  # raises on illegal shapes
        # Only now, after validation, touch the filesystem.
        directory.mkdir(parents=True, exist_ok=True)

        if db_path.exists():
            # Rebuild from scratch: old file would mix schemas.
            sqlite3.connect(str(db_path)).close()
            db_path.unlink()
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

        connection = sqlite3.connect(str(db_path))
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.executescript(
                '''
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE documents(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE indexes_meta(
                    name TEXT PRIMARY KEY,
                    paths_json TEXT NOT NULL,
                    table_name TEXT NOT NULL
                );
                CREATE TABLE query_log(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    query_json TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    ranges INTEGER NOT NULL,
                    keys_accessed INTEGER NOT NULL,
                    candidates INTEGER NOT NULL,
                    fetches INTEGER NOT NULL,
                    residual_checks INTEGER NOT NULL,
                    matches INTEGER NOT NULL
                );
                '''
            )
            created_at = datetime.now(timezone.utc).isoformat()
            connection.execute('INSERT INTO meta VALUES(?,?)', ('created_at', created_at))

            for name, spec in named_specs:
                connection.execute(_create_index_sql(name, spec))
                connection.execute(
                    'INSERT INTO indexes_meta(name, paths_json, table_name) VALUES(?,?,?)',
                    (name, json.dumps(spec.paths), _table_name(name)),
                )

            for document in documents:
                validate_document(document)
            with connection:
                for document in documents:
                    connection.execute(
                        'INSERT INTO documents(id, payload) VALUES(?,?)',
                        (document['_id'],
                         json.dumps(document, ensure_ascii=False, sort_keys=True)),
                    )
                for document in documents:
                    _insert_document(connection, named_specs, document)
        except Exception:
            connection.close()
            db_path.unlink(missing_ok=True)
            raise
        connection.close()
        return cls(directory)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _load_specs(self):
        rows = self.connection.execute(
            'SELECT name, paths_json FROM indexes_meta ORDER BY name'
        ).fetchall()
        return [(row['name'], IndexSpec(json.loads(row['paths_json']))) for row in rows]

    @property
    def index_specs(self):
        return [(name, list(spec.paths)) for name, spec in self._specs]

    # ---------------------------------------------------------------- writes
    def put(self, document):
        """Whole-document replace: old keys leave, new keys enter in one txn."""
        validate_document(document)
        doc_id = document['_id']
        conn = self.connection
        try:
            conn.execute('BEGIN IMMEDIATE')
            _delete_document_indexes(conn, self._specs, doc_id)
            conn.execute('INSERT OR REPLACE INTO documents(id, payload) VALUES(?,?)',
                         (doc_id, json.dumps(document, ensure_ascii=False, sort_keys=True)))
            _insert_document(conn, self._specs, document)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def remove(self, doc_id):
        conn = self.connection
        try:
            conn.execute('BEGIN IMMEDIATE')
            existed = conn.execute('SELECT 1 FROM documents WHERE id=?', (doc_id,)).fetchone()
            _delete_document_indexes(conn, self._specs, doc_id)
            conn.execute('DELETE FROM documents WHERE id=?', (doc_id,))
            conn.commit()
            return existed is not None
        except Exception:
            conn.rollback()
            raise

    # ---------------------------------------------------------------- reads
    def query(self, ast):
        explanation = self.explain(ast)
        return explanation['matches']

    def explain(self, ast):
        plan = plan_query(ast, self._specs)
        started_at = datetime.now(timezone.utc).isoformat()

        if plan['strategy'] == 'full_scan':
            doc_ids = [
                row[0]
                for row in self.connection.execute('SELECT id FROM documents ORDER BY id')
            ]
            traces = []
            ranges = 0
            keys_accessed = 0
            post_setop_candidates = set(doc_ids)
        else:
            traces = []
            post_setop_candidates = self._execute_tree(plan['tree'], traces)
            ranges = sum(trace.get('ranges', 0) for trace in traces)
            keys_accessed = sum(trace.get('keys_accessed', 0) for trace in traces)

        # Residual pass: candidate sets are supersets, the AST is the truth.
        payload_cache = {}
        residual_checks = 0
        matches = []
        for doc_id in post_setop_candidates:
            document = payload_cache.get(doc_id)
            if document is None:
                row = self.connection.execute(
                    'SELECT payload FROM documents WHERE id=?', (doc_id,)
                ).fetchone()
                if row is None:
                    # Stale-looking candidate should never happen; skip safely.
                    continue
                document = json.loads(row['payload'])
                payload_cache[doc_id] = document
            residual_checks += 1
            if evaluate(ast, document):
                matches.append(doc_id)
        matches.sort()

        explanation = {
            'query': ast,
            'strategy': plan['strategy'],
            'full_scan_reason': plan['full_scan_reason'],
            'candidate_indexes': plan['candidate_indexes'],
            'key_bounds': _summarize_bounds(plan['tree']),
            'element_binding': _summarize_binding(plan['tree']),
            'scan_traces': traces,
            'ranges': ranges,
            'keys_accessed': keys_accessed,
            'candidates': len(post_setop_candidates),
            'fetches': len(payload_cache),
            'residual_checks': residual_checks,
            'residual_conditions': ast,
            'matches': matches,
            'started_at': started_at,
        }
        self.connection.execute(
            '''INSERT INTO query_log(started_at, query_json, strategy, ranges,
                                     keys_accessed, candidates, fetches,
                                     residual_checks, matches)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            (started_at, json.dumps(ast, ensure_ascii=False, sort_keys=True),
             plan['strategy'], ranges, keys_accessed, len(post_setop_candidates),
             len(payload_cache), residual_checks, len(matches)),
        )
        self.connection.commit()
        self.last_explain = explanation
        return explanation

    def query_log(self, limit=50):
        rows = self.connection.execute(
            'SELECT * FROM query_log ORDER BY seq DESC LIMIT ?', (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def index_access_log(self, limit=50):
        """The retained index access log (same rows, explicit name)."""
        return self.query_log(limit)

    def documents(self):
        return [
            json.loads(row['payload'])
            for row in self.connection.execute('SELECT payload FROM documents ORDER BY id')
        ]

    def dump_index(self, index_name):
        """Inspect typed postings; supports tests and manual debugging."""
        table = _table_name(index_name)
        spec = dict((name, spec) for name, spec in self._specs)[index_name]
        columns = ', '.join('t%d, v%d, doc_id' % (i, i) for i in range(len(spec.paths)))
        rows = self.connection.execute(
            'SELECT %s FROM %s ORDER BY %s' %
            (columns, table, ', '.join('t%d, v%d' % (i, i) for i in range(len(spec.paths))) + ', doc_id')
        ).fetchall()
        result = []
        for row in rows:
            key = [
                (row['t%d' % i], _from_storage(row['t%d' % i], row['v%d' % i]))
                for i in range(len(spec.paths))
            ]
            result.append({'key': key, 'doc_id': row['doc_id']})
        return result

    # ------------------------------------------------------------- execution
    def _execute_tree(self, tree, traces):
        if tree['op'] == 'scan':
            ids, trace = self._execute_scan(tree)
            traces.append(trace)
            return ids
        child_sets = [self._execute_tree(child, traces) for child in tree['children']]
        if tree['op'] == 'intersect':
            result = set.intersection(*child_sets) if child_sets else set()
        elif tree['op'] == 'union':
            result = set().union(*child_sets)
        else:
            raise AssertionError('unknown tree op %r' % tree['op'])
        traces.append({
            'op': tree['op'],
            'reason': tree.get('reason'),
            'input_sizes': [len(child) for child in child_sets],
            'output_size': len(result),
        })
        return result

    def _execute_scan(self, scan):
        spec = self._spec_by_name(scan['index_name'])
        table = _table_name(scan['index_name'])
        width = len(spec.paths)

        clauses = []
        parameters = []
        for item in scan['bounds']:
            clause, params = _render_bound(item['slot'], item['bound'])
            clauses.append(clause)
            parameters.extend(params)

        # Leading root gaps: enumerate distinct root prefix values and run one
        # range per combination.  These gaps are root scalars only, so no
        # cartesian array combination is introduced.
        gap_values = {}
        for slot in scan['expanded_root_gaps']:
            where_sql = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            rows = self.connection.execute(
                'SELECT DISTINCT t%d, v%d FROM %s%s ORDER BY t%d, v%d'
                % (slot, slot, table, where_sql, slot, slot),
                parameters,
            ).fetchall()
            gap_values[slot] = [(row[0], row[1]) for row in rows]

        select_cols = ', '.join('t%d, v%d' % (i, i) for i in range(width))
        ranges = 0
        keys_accessed = 0
        doc_ids = set()

        if scan['expanded_root_gaps']:
            combinations = [{}]
            for slot in scan['expanded_root_gaps']:
                combinations = [
                    {**combo, slot: value}
                    for combo in combinations
                    for value in gap_values[slot]
                ]
            for combo in combinations:
                combo_clauses = list(clauses)
                combo_params = list(parameters)
                for slot, (tag, value) in combo.items():
                    combo_clauses.append('t%d = ?' % slot)
                    combo_params.append(tag)
                    if value is not None:
                        combo_clauses.append('v%d = ?' % slot)
                        combo_params.append(value)
                ranges += 1
                rows = self.connection.execute(
                    'SELECT %s, doc_id FROM %s WHERE %s'
                    % (select_cols, table, ' AND '.join(combo_clauses)),
                    combo_params,
                ).fetchall()
                keys_accessed += len(rows)
                doc_ids.update(row['doc_id'] for row in rows)
        else:
            where_sql = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            ranges = 1
            rows = self.connection.execute(
                'SELECT %s, doc_id FROM %s%s' % (select_cols, table, where_sql),
                parameters,
            ).fetchall()
            keys_accessed = len(rows)
            doc_ids.update(row['doc_id'] for row in rows)

        trace = {
            'op': 'scan',
            'index_name': scan['index_name'],
            'correlated': scan['correlated'],
            'binding': scan['binding'],
            'bounds': scan['bounds'],
            'expanded_root_gaps': scan['expanded_root_gaps'],
            'ranges': ranges,
            'keys_accessed': keys_accessed,
            'candidate_docs': len(doc_ids),
        }
        return doc_ids, trace

    def _spec_by_name(self, name):
        for candidate_name, spec in self._specs:
            if candidate_name == name:
                return spec
        raise KeyError(name)

    # ---------------------------------------------------------------- verify
    def verify(self):
        """Full consistency check: stored postings == re-extracted postings."""
        conn = self.connection
        problems = []
        stored_doc_ids = {row[0] for row in conn.execute('SELECT id FROM documents')}

        for name, spec in self._specs:
            table = _table_name(name)
            stored = {}
            for row in conn.execute('SELECT * FROM %s' % table):
                key = tuple(
                    (row['t%d' % i], _from_storage(row['t%d' % i], row['v%d' % i]))
                    for i in range(len(spec.paths))
                )
                stored.setdefault(row['doc_id'], set()).add(key)

            # Orphans: postings without a document.
            for doc_id in stored:
                if doc_id not in stored_doc_ids:
                    problems.append('index %r has orphan posting for removed doc %r' % (name, doc_id))

            expected = {}
            for doc_id in stored_doc_ids:
                payload = json.loads(
                    conn.execute('SELECT payload FROM documents WHERE id=?', (doc_id,)).fetchone()[0]
                )
                expected[doc_id] = set(tuple(pair) for pair in extract_keys(payload, spec))

            if set(stored) - stored_doc_ids:
                pass  # already reported as orphans
            for doc_id in stored_doc_ids:
                if stored.get(doc_id, set()) != expected[doc_id]:
                    problems.append(
                        'index %r mismatch for doc %r: stored=%r expected=%r'
                        % (name, doc_id, sorted(stored.get(doc_id, set())), sorted(expected[doc_id]))
                    )

        # Every stored payload must itself be a valid document.
        for doc_id in stored_doc_ids:
            payload = json.loads(
                conn.execute('SELECT payload FROM documents WHERE id=?', (doc_id,)).fetchone()[0]
            )
            try:
                validate_document(payload)
            except ValueError as exc:
                problems.append('document %r invalid: %s' % (doc_id, exc))

        return {
            'ok': not problems,
            'documents': len(stored_doc_ids),
            'indexes': [name for name, _ in self._specs],
            'problems': problems,
        }


# --------------------------------------------------------------------- helpers
def _normalize_indexes(indexes):
    # Bare list of path strings -> one index named idx0; list of path lists
    # -> several indexes; mapping -> explicit name -> paths.
    if isinstance(indexes, list) and indexes and all(isinstance(path, str) for path in indexes):
        indexes = [indexes]
    if isinstance(indexes, dict):
        items = sorted(indexes.items())
    else:
        items = [('idx%d' % i, paths) for i, paths in enumerate(indexes)]
    normalized = []
    for name, paths in items:
        if not _NAME_RE.match(name):
            raise ValueError('illegal index name: %r' % name)
        normalized.append((name, IndexSpec(paths)))  # validation raises here
    if not normalized:
        raise ValueError('at least one index is required')
    return normalized


def _create_index_sql(index_name, spec):
    table = _table_name(index_name)
    columns = []
    for slot in range(len(spec.paths)):
        columns.append('t%d TEXT NOT NULL' % slot)
        # NOT NULL payload column: tag-only slots store the empty sentinel,
        # so primary-key deduplication still works for every type.
        columns.append('v%d NOT NULL' % slot)
    columns.append('doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE')
    pk = ', '.join(['t%d, v%d' % (i, i) for i in range(len(spec.paths))] + ['doc_id'])
    return (
        'CREATE TABLE %s (%s, PRIMARY KEY (%s)) WITHOUT ROWID'
        % (table, ', '.join(columns), pk)
    )


def _insert_document(connection, named_specs, document):
    doc_id = document['_id']
    for name, spec in named_specs:
        table = _table_name(name)
        placeholders = ', '.join(['?'] * (len(spec.paths) * 2 + 1))
        for typed_key in extract_keys(document, spec):
            values = []
            for tag, value in typed_key:
                values.extend((tag, _to_storage(tag, value)))
            values.append(doc_id)
            # De-duplicated posting: same key twice in one document is one row.
            connection.execute(
                'INSERT OR IGNORE INTO %s VALUES(%s)' % (table, placeholders), values
            )


def _delete_document_indexes(connection, named_specs, doc_id):
    for name, _ in named_specs:
        connection.execute(
            'DELETE FROM %s WHERE doc_id=?' % _table_name(name), (doc_id,)
        )


def _render_bound(slot, bound):
    op = bound['op']
    if op == 'eq':
        if bound['value'] is None:
            return 't%d = ?' % slot, [bound['tag']]
        return 't%d = ? AND v%d = ?' % (slot, slot), [bound['tag'], bound['value']]
    if op in ('ge', 'le'):
        comparator = '>=' if op == 'ge' else '<='
        return 't%d = ? AND v%d %s ?' % (slot, slot, comparator), [bound['tag'], bound['value']]
    if op == 'tag_not_in':
        marks = ', '.join('?' for _ in bound['exclude_tags'])
        return 't%d NOT IN (%s)' % (slot, marks), list(bound['exclude_tags'])
    raise AssertionError('unknown bound op %r' % op)


def _summarize_bounds(tree):
    if tree is None:
        return []
    return [
        {
            'index_name': scan['index_name'],
            'correlated': scan['correlated'],
            'binding': scan['binding'],
            'bounds': scan['bounds'],
            'expanded_root_gaps': scan['expanded_root_gaps'],
        }
        for scan in collect_scans(tree)
    ]


def _summarize_binding(tree):
    if tree is None:
        return {'correlated_scans': [], 'independent_scans': []}
    scans = collect_scans(tree)
    return {
        'correlated_scans': [
            {'index_name': scan['index_name'], 'basis': scan['binding'],
             'fields': [item['field'] for item in scan['bounds']]}
            for scan in scans if scan['correlated']
        ],
        'independent_scans': [
            {'index_name': scan['index_name'], 'basis': scan['binding'],
             'fields': [item['field'] for item in scan['bounds']]}
            for scan in scans if not scan['correlated']
        ],
        'rule': 'same-element bounds are allowed only inside one elemMatch block; '
                'plain AND leaves combine at document-posting level',
    }
