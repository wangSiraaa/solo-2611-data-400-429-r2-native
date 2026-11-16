"""Shared bounded-state executor for data-quality checks.

Keeps the original probe API (evaluate, run) and adds execute_plan(), which
runs the compiler's shared plan: one real read per involved source, per-check
population masks (each check keeps its own denominator), SQLite-backed staging
for duplicate groups and reference key domains, and a 64-row resident budget
covering decoded rows, group payloads and pending witnesses.  SQLite's own
page cache is capped at 4 MiB and disclosed separately; it never counts
against the 64-row budget, and staged rows are streamed back one at a time.
"""
import json
import sqlite3
from pathlib import Path

from compiler import compile_checks, compile_plan, json_type, matches, ConfigError
from source import Source

RESIDENT_LIMIT = 64          # decoded rows + group payloads + pending witnesses
PAGE_CACHE_KIB = 4096        # 4 MiB SQLite page cache, disclosed separately
MAX_PAGE_CACHE_KIB = 4096
CHUNK_SIZE = 8               # rows decoded per read batch before dispatch
WITNESS_LIMIT = 3            # smallest-row_id witnesses kept per check

_TYPE_RANK = {'null': 0, 'bool': 1, 'int': 2, 'text': 3}


class CorruptSource(Exception):
    """A source file is unreadable or violates its declared schema."""


class _Budget:
    """Resident-row accounting: decoded rows, group payloads, witnesses."""

    def __init__(self, limit):
        self.limit = limit
        self.current = 0
        self.peak = 0
        self.peak_components = {}
        self._components = {}

    def acquire(self, component, n=1):
        self._components[component] = self._components.get(component, 0) + n
        self.current += n
        if self.current > self.peak:
            self.peak = self.current
            self.peak_components = dict(self._components)
        if self.current > self.limit:
            raise RuntimeError('resident budget exceeded: %d > %d rows' % (self.current, self.limit))

    def release(self, component, n=1):
        self._components[component] -= n
        self.current -= n


# --- legacy probe API (fixed: per-check masks, one shared scan) ---

def evaluate(check, rows):
    if check['kind'] == 'not_null':
        bad = [r['row_id'] for r in rows if r.get(check['field']) is None]
    elif check['kind'] == 'unique':
        groups = {}
        for row in rows:
            groups.setdefault(tuple(row.get(c) for c in check['fields']), []).append(row['row_id'])
        bad = [row_id for ids in groups.values() if len(ids) > 1 for row_id in ids]
    else:
        raise ValueError('unknown check kind')
    return {'check_id': check['check_id'], 'population_count': len(rows),
            'violation_count': len(bad), 'violating_rows': sorted(bad), 'passed': not bad}


def run(source, checks):
    """One shared scan; every check is evaluated only against its own population."""
    plan = compile_checks(checks)
    buckets = [[] for _ in plan['checks']]
    for row in source.scan():
        for index, mask in enumerate(plan['masks']):
            if mask(row):
                buckets[index].append(row)
    return [evaluate(check, bucket) for check, bucket in zip(plan['checks'], buckets)]


# --- shared bounded-state executor ---

def incomplete_result(phase, message, plan=None, checks=None):
    entries = []
    if isinstance(checks, list):
        for raw in checks:
            entries.append({'check_id': raw.get('check_id') if isinstance(raw, dict) else None,
                            'status': 'not_evaluated', 'population_count': None,
                            'evaluated_count': None, 'violation_count': None,
                            'passed': None, 'witnesses': []})
    return {'status': 'incomplete', 'all_pass': None,
            'error': {'phase': phase, 'message': message},
            'plan': plan, 'checks': entries, 'sources': {},
            'staging': {'writes': 0, 'reads': 0, 'by_table': {}},
            'resident': {'limit_rows': RESIDENT_LIMIT, 'peak_rows': 0, 'peak_components': {}},
            'sqlite': {'page_cache_kib': PAGE_CACHE_KIB, 'page_cache_bytes': PAGE_CACHE_KIB * 1024,
                       'temp_store': 'FILE', 'state_file_bytes': 0}}


def execute_plan(catalog, checks, base_dir, out_dir, *, chunk_size=CHUNK_SIZE,
                 resident_limit=RESIDENT_LIMIT, page_cache_kib=PAGE_CACHE_KIB):
    """Executes the shared plan for catalog+checks and returns the run result."""
    if page_cache_kib > MAX_PAGE_CACHE_KIB:
        raise ValueError('page cache above the 4 MiB disclosure limit')
    try:
        plan = compile_plan(catalog, checks)
    except ConfigError as error:
        return incomplete_result('compile', str(error), checks=checks)
    try:
        return _execute(plan, Path(base_dir), Path(out_dir), chunk_size,
                        resident_limit, page_cache_kib)
    except Exception as error:  # a run must report incomplete, never crash silently
        return incomplete_result('execute', '%s: %s' % (type(error).__name__, error),
                                 plan=plan, checks=plan['checks'])


def _row_id_affinity(schema):
    return 'TEXT' if 'text' in schema['row_id'] else 'INTEGER'


def _rid_key(row_id):
    return (_TYPE_RANK[json_type(row_id)], row_id)


def _enc_key(parts):
    """Typed composite key encoding: NULL/bool/int/text stay distinct."""
    return json.dumps([[json_type(part), part] for part in parts],
                      separators=(',', ':'), ensure_ascii=False)


def _dec_key(enc):
    return [value for _kind, value in json.loads(enc)]


def _typed(value):
    return {'type': json_type(value), 'value': value}


def _path(check):
    return '/checks/%s' % check['check_id']


def _witness_not_null(check, row_id):
    return {'assertion': 'not_null', 'check_id': check['check_id'], 'source': check['source'],
            'row_id': row_id, 'path': _path(check), 'field': check['assertion']['field']}


def _witness_unique(check, row_id, parts, partner):
    return {'assertion': 'unique', 'check_id': check['check_id'], 'source': check['source'],
            'row_id': row_id, 'path': _path(check), 'fields': check['assertion']['fields'],
            'key': [_typed(part) for part in parts], 'partner': partner}


def _witness_reference(check, row_id, parts):
    assertion = check['assertion']
    return {'assertion': 'reference', 'check_id': check['check_id'], 'source': check['source'],
            'row_id': row_id, 'path': _path(check), 'target_source': assertion['target_source'],
            'key': {'fields': list(assertion['source_fields']),
                    'values': [_typed(part) for part in parts]}}


def _new_cstate():
    return {'population_count': 0, 'evaluated_count': 0, 'violation_count': 0,
            'skipped_null_rows': 0, 'duplicate_groups': 0, 'witnesses': [],
            'target': {'matched_rows': 0, 'null_key_rows_excluded': 0,
                       'distinct_keys': 0, 'duplicate_key_rows': 0}}


def _stage_write(staging, table):
    staging['writes'] += 1
    staging['by_table'].setdefault(table, {'writes': 0, 'reads': 0})['writes'] += 1


def _stage_read(staging, table):
    staging['reads'] += 1
    staging['by_table'].setdefault(table, {'writes': 0, 'reads': 0})['reads'] += 1


def _stream(cursor, staging, budget, table):
    """Streams staged rows back one at a time; each counts as a resident payload."""
    for row in cursor:
        _stage_read(staging, table)
        budget.acquire('group_payloads', 1)
        try:
            yield row
        finally:
            budget.release('group_payloads', 1)


def _chunks(iterator, size):
    chunk = []
    for item in iterator:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _validate_row(row, schema, source_name):
    if not isinstance(row, dict):
        raise CorruptSource('%s: row is not a JSON object' % source_name)
    for field, declared in schema.items():
        value = row.get(field)
        try:
            kind = json_type(value)
        except ValueError:
            raise CorruptSource('%s: field %r holds a non-leaf value (row_id %r)'
                                % (source_name, field, row.get('row_id')))
        if kind not in declared:
            raise CorruptSource('%s: field %r holds type %s, declared %s (row_id %r)'
                                % (source_name, field, kind, sorted(declared), row.get('row_id')))


def _offer_witness(st, budget, witness):
    """Keeps the WITNESS_LIMIT smallest-row_id witnesses in a bounded buffer."""
    key = _rid_key(witness['row_id'])
    buffer = st['witnesses']
    position = 0
    while position < len(buffer) and _rid_key(buffer[position]['row_id']) < key:
        position += 1
    if position >= WITNESS_LIMIT:
        return
    buffer.insert(position, witness)
    budget.acquire('witnesses', 1)
    while len(buffer) > WITNESS_LIMIT:
        buffer.pop()
        budget.release('witnesses', 1)


def _dispatch_check(db, staging, budget, ci, check, st, row):
    if not matches(row, check['population']):
        return
    st['population_count'] += 1
    assertion = check['assertion']
    kind = check['kind']
    if kind == 'not_null':
        st['evaluated_count'] += 1
        if row.get(assertion['field']) is None:
            st['violation_count'] += 1
            _offer_witness(st, budget, _witness_not_null(check, row['row_id']))
    elif kind == 'unique':
        parts = [row.get(field) for field in assertion['fields']]
        if assertion['nulls'] == 'ignore' and any(part is None for part in parts):
            st['skipped_null_rows'] += 1  # still in population_count, not evaluated
            return
        st['evaluated_count'] += 1
        db.execute('INSERT INTO uq_%d VALUES (?,?)' % ci, (_enc_key(parts), row['row_id']))
        _stage_write(staging, 'uq_%d' % ci)
    else:  # reference, source side
        parts = [row.get(field) for field in assertion['source_fields']]
        if any(part is None for part in parts):
            if assertion['nulls'] == 'ignore':
                st['skipped_null_rows'] += 1
                return
            st['evaluated_count'] += 1  # nulls=reject: evaluated and failed, no SQL UNKNOWN
            st['violation_count'] += 1
            _offer_witness(st, budget, _witness_reference(check, row['row_id'], parts))
            return
        st['evaluated_count'] += 1
        db.execute('INSERT INTO rs_%d VALUES (?,?)' % ci, (row['row_id'], _enc_key(parts)))
        _stage_write(staging, 'rs_%d' % ci)


def _dispatch_target(db, staging, ci, check, st, row):
    assertion = check['assertion']
    if not matches(row, assertion['target_population']):
        return
    target = st['target']
    target['matched_rows'] += 1
    parts = [row.get(field) for field in assertion['target_fields']]
    if any(part is None for part in parts):
        target['null_key_rows_excluded'] += 1
        return
    cursor = db.execute('INSERT OR IGNORE INTO rt_%d VALUES (?)' % ci, (_enc_key(parts),))
    _stage_write(staging, 'rt_%d' % ci)
    if cursor.rowcount:
        target['distinct_keys'] += 1
    else:
        target['duplicate_key_rows'] += 1  # duplicates collapse, never amplify


def _finalize_unique(db, staging, budget, ci, check, st):
    table = 'uq_%d' % ci
    cursor = db.execute('SELECT k, COUNT(*) FROM %s GROUP BY k HAVING COUNT(*) > 1' % table)
    for _enc, n in _stream(cursor, staging, budget, table):
        st['duplicate_groups'] += 1
        st['violation_count'] += n  # a group of size k counts k rows, not k-1
    cursor = db.execute('SELECT row_id, k FROM %s WHERE k IN '
                        '(SELECT k FROM %s GROUP BY k HAVING COUNT(*) > 1) '
                        'ORDER BY row_id LIMIT %d' % (table, table, WITNESS_LIMIT))
    for row_id, enc in _stream(cursor, staging, budget, table):
        partner = db.execute('SELECT row_id FROM %s WHERE k = ? AND row_id <> ? '
                             'ORDER BY row_id LIMIT 1' % table, (enc, row_id)).fetchone()[0]
        _stage_read(staging, table)
        st['witnesses'].append(_witness_unique(check, row_id, _dec_key(enc), partner))
        budget.acquire('witnesses', 1)


def _finalize_reference(db, staging, budget, ci, check, st):
    rs, rt = 'rs_%d' % ci, 'rt_%d' % ci
    cursor = db.execute('SELECT s.row_id, s.k FROM %s s WHERE NOT EXISTS '
                        '(SELECT 1 FROM %s t WHERE t.k = s.k) ORDER BY s.row_id' % (rs, rt))
    buffered = st['witnesses']  # nulls=reject violations, already smallest-first
    acquired = len(buffered)    # each buffered witness was acquired at offer time
    st['witnesses'] = []
    merged = []
    for row_id, enc in _stream(cursor, staging, budget, rs):
        st['violation_count'] += 1  # every missing source row counts independently
        while buffered and len(merged) < WITNESS_LIMIT and _rid_key(buffered[0]['row_id']) < _rid_key(row_id):
            merged.append(buffered.pop(0))
        if len(merged) < WITNESS_LIMIT:
            merged.append(_witness_reference(check, row_id, _dec_key(enc)))
    while buffered and len(merged) < WITNESS_LIMIT:
        merged.append(buffered.pop(0))
    for _ in range(acquired):
        budget.release('witnesses', 1)
    for _ in merged:
        budget.acquire('witnesses', 1)
    st['witnesses'] = merged


def _detail(check, st):
    kind = check['kind']
    if kind == 'not_null':
        return {'field': check['assertion']['field']}
    if kind == 'unique':
        return {'fields': check['assertion']['fields'], 'nulls': check['assertion']['nulls'],
                'skipped_null_rows': st['skipped_null_rows'],
                'duplicate_groups': st['duplicate_groups']}
    return {'source_fields': check['assertion']['source_fields'],
            'target_fields': check['assertion']['target_fields'],
            'nulls': check['assertion']['nulls'],
            'skipped_null_rows': st['skipped_null_rows'], 'target': st['target']}


def _execute(plan, base_dir, out_dir, chunk_size, resident_limit, page_cache_kib):
    budget = _Budget(resident_limit)
    staging = {'writes': 0, 'reads': 0, 'by_table': {}}
    checks = plan['checks']
    cstate = {check['check_id']: _new_cstate() for check in checks}
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / 'state.sqlite'
    if state_path.exists():
        state_path.unlink()
    db = sqlite3.connect(str(state_path))
    error = None
    scan_status = {}
    sources_counter = {}
    try:
        db.execute('PRAGMA cache_size=-%d' % page_cache_kib)  # 4 MiB, disclosed separately
        db.execute('PRAGMA temp_store=FILE')
        seen_table = {}
        for index, scan in enumerate(plan['scans']):
            table = 'seen_%d' % index
            affinity = _row_id_affinity(plan['sources'][scan['source']]['schema'])
            db.execute('CREATE TABLE %s (row_id %s PRIMARY KEY)' % (table, affinity))
            seen_table[scan['scan_id']] = table
        for ci, check in enumerate(checks):
            affinity = _row_id_affinity(plan['sources'][check['source']]['schema'])
            if check['kind'] == 'unique':
                db.execute('CREATE TABLE uq_%d (k TEXT NOT NULL, row_id %s NOT NULL)' % (ci, affinity))
                db.execute('CREATE INDEX uq_%d_by_k ON uq_%d (k)' % (ci, ci))
            elif check['kind'] == 'reference':
                db.execute('CREATE TABLE rs_%d (row_id %s NOT NULL, k TEXT NOT NULL)' % (ci, affinity))
                db.execute('CREATE TABLE rt_%d (k TEXT PRIMARY KEY)' % ci)
        consumers = {scan['scan_id']: {'checks': [], 'targets': []} for scan in plan['scans']}
        for ci, check in enumerate(checks):
            consumers[check['scan_id']]['checks'].append(ci)
            if check['kind'] == 'reference':
                consumers[check['assertion']['target_scan_id']]['targets'].append(ci)
        for scan in plan['scans']:
            scan_id, name = scan['scan_id'], scan['source']
            spec = plan['sources'][name]
            source = Source(base_dir / spec['file'])
            try:
                for chunk in _chunks(source.scan(), chunk_size):
                    budget.acquire('decoded_rows', len(chunk))
                    try:
                        for row in chunk:
                            _validate_row(row, spec['schema'], name)
                            table = seen_table[scan_id]
                            cursor = db.execute('INSERT OR IGNORE INTO %s VALUES (?)' % table,
                                                (row['row_id'],))
                            _stage_write(staging, table)
                            if cursor.rowcount == 0:
                                raise CorruptSource('%s: duplicate row_id %r' % (name, row['row_id']))
                            for ci in consumers[scan_id]['checks']:
                                _dispatch_check(db, staging, budget, ci, checks[ci],
                                                cstate[checks[ci]['check_id']], row)
                            for ci in consumers[scan_id]['targets']:
                                _dispatch_target(db, staging, ci, checks[ci],
                                                 cstate[checks[ci]['check_id']], row)
                    finally:
                        budget.release('decoded_rows', len(chunk))
                scan_status[scan_id] = True
            except (CorruptSource, OSError, json.JSONDecodeError) as exc:
                scan_status[scan_id] = False
                if error is None:
                    error = {'phase': 'scan', 'source': name, 'message': str(exc)}
            sources_counter[name] = {'file': spec['file'], 'scans': source.scans,
                                     'decoded': source.decoded,
                                     'scan_complete': scan_status[scan_id]}
        results = []
        for ci, check in enumerate(checks):
            st = cstate[check['check_id']]
            if not all(scan_status.get(scan_id, False) for scan_id in check['required_scans']):
                results.append({'check_id': check['check_id'], 'kind': check['kind'],
                                'source': check['source'], 'status': 'not_evaluated',
                                'population_count': None, 'evaluated_count': None,
                                'violation_count': None, 'passed': None, 'witnesses': []})
                continue
            if check['kind'] == 'unique':
                _finalize_unique(db, staging, budget, ci, check, st)
            elif check['kind'] == 'reference':
                _finalize_reference(db, staging, budget, ci, check, st)
            results.append({'check_id': check['check_id'], 'kind': check['kind'],
                            'source': check['source'], 'status': 'evaluated',
                            'population_count': st['population_count'],
                            'evaluated_count': st['evaluated_count'],
                            'violation_count': st['violation_count'],
                            'passed': st['violation_count'] == 0,
                            'witnesses': st['witnesses'], 'detail': _detail(check, st)})
        complete = error is None and all(scan_status.values())
        db.commit()
    finally:
        db.close()
    state_bytes = state_path.stat().st_size if state_path.exists() else 0
    evaluated = [entry for entry in results if entry['status'] == 'evaluated']
    return {'status': 'complete' if complete else 'incomplete',
            'all_pass': all(entry['passed'] for entry in results) if complete else None,
            'error': error, 'plan': plan, 'checks': results, 'sources': sources_counter,
            'staging': staging,
            'resident': {'limit_rows': budget.limit, 'peak_rows': budget.peak,
                         'peak_components': budget.peak_components},
            'sqlite': {'page_cache_kib': page_cache_kib, 'page_cache_bytes': page_cache_kib * 1024,
                       'temp_store': 'FILE', 'state_file_bytes': state_bytes},
            'run': {'chunk_size': chunk_size, 'witness_limit': WITNESS_LIMIT,
                    'checks_total': len(results), 'checks_evaluated': len(evaluated),
                    'checks_passed': sum(1 for entry in evaluated if entry['passed'])}}
