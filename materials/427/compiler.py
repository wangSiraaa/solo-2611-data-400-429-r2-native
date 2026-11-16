"""Check compiler: validates check configurations and plans shared scans.

Keeps the original probe API (matches, compile_checks) and adds
compile_plan(), which validates a catalog plus check list *before any source
row is read* and emits the shared-scan plan: one scan per involved source,
the columns each scan reads, and the responsibility boundary of every
population and every reference target key domain.  Each population constrains
only its own check; the planner never merges data domains across checks.
"""

LEAF_TYPES = ('null', 'bool', 'int', 'text')
PREDICATE_OPS = ('all', 'eq', 'in', 'is_null', 'and', 'or', 'not')
ASSERTION_KINDS = ('not_null', 'unique', 'reference')
UNIQUE_NULLS = ('equal', 'ignore')
REFERENCE_NULLS = ('ignore', 'reject')


class ConfigError(Exception):
    """Invalid catalog or check configuration, raised before any source read."""


def json_type(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int):
        return 'int'
    if isinstance(value, str):
        return 'text'
    raise ValueError('not a JSON leaf (null/bool/int/text): %r' % (value,))


def typed_eq(left, right):
    """Exact JSON equality: bool/int/text/null stay distinct (true != 1)."""
    try:
        return json_type(left) == json_type(right) and left == right
    except ValueError:
        return False


def matches(row, predicate):
    op = predicate['op']
    if op == 'all':
        return True
    if op == 'eq':
        return typed_eq(row.get(predicate['field']), predicate['value'])
    if op == 'in':
        return any(typed_eq(row.get(predicate['field']), value) for value in predicate['values'])
    if op == 'is_null':
        return row.get(predicate['field']) is None
    if op == 'and':
        return all(matches(row, arg) for arg in predicate['args'])
    if op == 'or':
        return any(matches(row, arg) for arg in predicate['args'])
    if op == 'not':
        return not matches(row, predicate['arg'])
    raise ValueError('unknown predicate')


def render_literal(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    import json
    return json.dumps(value, ensure_ascii=False)


def render(predicate):
    """Canonical text form of a predicate, used for responsibility boundaries."""
    op = predicate['op']
    if op == 'all':
        return 'all()'
    if op == 'eq':
        return 'eq(%s,%s)' % (predicate['field'], render_literal(predicate['value']))
    if op == 'in':
        return 'in(%s,[%s])' % (predicate['field'], ','.join(render_literal(v) for v in predicate['values']))
    if op == 'is_null':
        return 'is_null(%s)' % predicate['field']
    if op == 'not':
        return 'not(%s)' % render(predicate['arg'])
    return '%s(%s)' % (op, ','.join(render(arg) for arg in predicate['args']))


def _validate_literal(value, declared, where):
    try:
        kind = json_type(value)
    except ValueError:
        raise ConfigError('%s: literal %r is not a JSON leaf (null/bool/int/text)' % (where, value))
    if kind == 'null':
        raise ConfigError('%s: NULL literals are only allowed via is_null' % where)
    if kind not in declared:
        raise ConfigError('%s: literal of type %s is incompatible with declared types %s'
                          % (where, kind, sorted(declared)))


def _validate_predicate(predicate, schema, where):
    """Validates a predicate against a source schema; returns the fields it reads."""
    if not isinstance(predicate, dict):
        raise ConfigError('%s: predicate must be an object' % where)
    op = predicate.get('op')
    if op not in PREDICATE_OPS:
        raise ConfigError('%s: unknown predicate op %r' % (where, op))
    allowed = {'all': {'op'}, 'eq': {'op', 'field', 'value'}, 'in': {'op', 'field', 'values'},
               'is_null': {'op', 'field'}, 'and': {'op', 'args'}, 'or': {'op', 'args'},
               'not': {'op', 'arg'}}[op]
    extra = set(predicate) - allowed
    if extra:
        raise ConfigError('%s: unexpected predicate keys %s' % (where, sorted(extra)))
    missing = allowed - {'op'} - set(predicate)
    if missing:
        raise ConfigError('%s: predicate %s is missing keys %s' % (where, op, sorted(missing)))
    if op == 'all':
        return set()
    if op in ('eq', 'in', 'is_null'):
        field = predicate['field']
        if not isinstance(field, str) or field not in schema:
            raise ConfigError('%s: unknown field %r' % (where, field))
        if op == 'eq':
            _validate_literal(predicate['value'], schema[field], where)
        elif op == 'in':
            values = predicate['values']
            if not isinstance(values, list) or not values:
                raise ConfigError('%s: in requires a non-empty values list' % where)
            for value in values:
                _validate_literal(value, schema[field], where)
        return {field}
    if op in ('and', 'or'):
        args = predicate['args']
        if not isinstance(args, list) or not args:
            raise ConfigError('%s: %s requires a non-empty args list' % (where, op))
        fields = set()
        for index, arg in enumerate(args):
            fields |= _validate_predicate(arg, schema, '%s.%s[%d]' % (where, op, index))
        return fields
    return _validate_predicate(predicate['arg'], schema, where + '.not')


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or not isinstance(catalog.get('sources'), dict) or not catalog['sources']:
        raise ConfigError('catalog must declare a non-empty "sources" object')
    sources = {}
    for name, spec in catalog['sources'].items():
        where = 'sources[%r]' % name
        if not isinstance(spec, dict) or not isinstance(spec.get('file'), str) or not spec['file']:
            raise ConfigError('%s: source must declare a file' % where)
        schema = spec.get('schema')
        if not isinstance(schema, dict) or not schema:
            raise ConfigError('%s: source must declare a non-empty schema' % where)
        if 'row_id' not in schema:
            raise ConfigError('%s: schema must declare the unique row_id field' % where)
        normalized = {}
        for field, declared in schema.items():
            types = declared if isinstance(declared, list) else [declared]
            if not types or any(t not in LEAF_TYPES for t in types):
                raise ConfigError('%s: field %r declares types outside %s' % (where, field, list(LEAF_TYPES)))
            normalized[field] = tuple(dict.fromkeys(types))
        sources[name] = {'file': spec['file'], 'schema': normalized}
    return sources


def _require_fields(fields, schema, where):
    for field in fields:
        if not isinstance(field, str) or field not in schema:
            raise ConfigError('%s: unknown field %r' % (where, field))


_CHECK_KEYS = {
    'not_null': {'check_id', 'kind', 'source', 'population', 'field'},
    'unique': {'check_id', 'kind', 'source', 'population', 'fields', 'nulls'},
    'reference': {'check_id', 'kind', 'source', 'population', 'target_source',
                  'target_population', 'source_fields', 'target_fields', 'nulls'},
}
_CHECK_REQUIRED = {
    'not_null': {'check_id', 'kind', 'source', 'population', 'field'},
    'unique': {'check_id', 'kind', 'source', 'population', 'fields'},
    'reference': {'check_id', 'kind', 'source', 'population', 'target_source',
                  'target_population', 'source_fields', 'target_fields'},
}


def _validate_check(raw, index, sources, seen_ids):
    where = 'checks[%d]' % index
    if not isinstance(raw, dict):
        raise ConfigError('%s: check must be an object' % where)
    kind = raw.get('kind')
    if kind not in ASSERTION_KINDS:
        raise ConfigError('%s: unknown check kind %r' % (where, kind))
    extra = set(raw) - _CHECK_KEYS[kind]
    if extra:
        raise ConfigError('%s: unexpected keys %s for a %s check' % (where, sorted(extra), kind))
    missing = _CHECK_REQUIRED[kind] - set(raw)
    if missing:
        raise ConfigError('%s: missing keys %s' % (where, sorted(missing)))
    check_id = raw['check_id']
    if not isinstance(check_id, str) or not check_id:
        raise ConfigError('%s: check_id must be a non-empty string' % where)
    if check_id in seen_ids:
        raise ConfigError('%s: duplicate check_id %r' % (where, check_id))
    seen_ids.add(check_id)
    source = raw['source']
    if not isinstance(source, str) or source not in sources:
        raise ConfigError('%s: unknown source %r' % (where, source))
    schema = sources[source]['schema']
    population_fields = _validate_predicate(raw['population'], schema, where + '.population')
    required_sources = [source]
    if kind == 'not_null':
        _require_fields([raw['field']], schema, where)
        assertion = {'kind': 'not_null', 'field': raw['field']}
        assertion_fields = [raw['field']]
    elif kind == 'unique':
        fields = raw['fields']
        if not isinstance(fields, list) or not fields:
            raise ConfigError('%s: unique fields must be a non-empty list' % where)
        _require_fields(fields, schema, where)
        nulls = raw.get('nulls', 'equal')
        if nulls not in UNIQUE_NULLS:
            raise ConfigError('%s: unique nulls must be one of %s' % (where, list(UNIQUE_NULLS)))
        assertion = {'kind': 'unique', 'fields': list(fields), 'nulls': nulls}
        assertion_fields = list(fields)
    else:
        target = raw['target_source']
        if not isinstance(target, str) or target not in sources:
            raise ConfigError('%s: unknown target_source %r' % (where, target))
        target_schema = sources[target]['schema']
        target_population_fields = _validate_predicate(raw['target_population'], target_schema,
                                                       where + '.target_population')
        source_fields, target_fields = raw['source_fields'], raw['target_fields']
        if (not isinstance(source_fields, list) or not isinstance(target_fields, list)
                or not source_fields or len(source_fields) != len(target_fields)):
            raise ConfigError('%s: source_fields and target_fields must be non-empty lists of equal length' % where)
        _require_fields(source_fields, schema, where)
        _require_fields(target_fields, target_schema, where)
        nulls = raw.get('nulls', 'ignore')
        if nulls not in REFERENCE_NULLS:
            raise ConfigError('%s: reference nulls must be one of %s' % (where, list(REFERENCE_NULLS)))
        assertion = {'kind': 'reference', 'target_source': target,
                     'target_population': raw['target_population'],
                     'target_population_canonical': render(raw['target_population']),
                     'target_population_fields': sorted(target_population_fields),
                     'source_fields': list(source_fields), 'target_fields': list(target_fields),
                     'nulls': nulls, 'target_scan_id': 'scan:' + target}
        assertion_fields = list(source_fields)
        required_sources.append(target)
    return {'check_id': check_id, 'kind': kind, 'source': source,
            'population': raw['population'], 'population_canonical': render(raw['population']),
            'population_fields': sorted(population_fields),
            'assertion': assertion, 'assertion_source_fields': sorted(set(assertion_fields)),
            'scan_id': 'scan:' + source,
            'required_scans': ['scan:' + name for name in required_sources]}


def compile_plan(catalog, checks):
    """Validates catalog + checks and emits the shared-scan plan (JSON-safe).

    Raises ConfigError before any source row is read when the configuration is
    illegal (unknown fields, illegal literals, bad policies, ...).
    """
    sources = validate_catalog(catalog)
    if not isinstance(checks, list) or not checks:
        raise ConfigError('checks must be a non-empty list')
    normalized = []
    seen_ids = set()
    for index, raw in enumerate(checks):
        normalized.append(_validate_check(raw, index, sources, seen_ids))
    scan_map = {}
    for check in normalized:
        scan = scan_map.setdefault(check['source'], {'scan_id': check['scan_id'], 'source': check['source'],
                                                     'file': sources[check['source']]['file'],
                                                     'read_columns': set(), 'consumers': []})
        uses = sorted(set(check['population_fields']) | set(check['assertion_source_fields']))
        scan['consumers'].append({
            'consumer': 'check:' + check['check_id'], 'role': 'population+assertion', 'uses_fields': uses,
            'responsibility': 'feeds check %s only: rows of %s matching %s'
                              % (check['check_id'], check['source'], check['population_canonical'])})
        scan['read_columns'].update(uses)
        if check['kind'] == 'reference':
            assertion = check['assertion']
            target = assertion['target_source']
            tscan = scan_map.setdefault(target, {'scan_id': assertion['target_scan_id'], 'source': target,
                                                 'file': sources[target]['file'],
                                                 'read_columns': set(), 'consumers': []})
            tuses = sorted(set(assertion['target_population_fields']) | set(assertion['target_fields']))
            tscan['consumers'].append({
                'consumer': 'reference_target:' + check['check_id'], 'role': 'target_key_domain',
                'uses_fields': tuses,
                'responsibility': 'feeds the key domain of check %s only: rows of %s matching %s'
                                  % (check['check_id'], target, assertion['target_population_canonical'])})
            tscan['read_columns'].update(tuses)
    scans = []
    for name in sorted(scan_map):
        scan = scan_map[name]
        scan['read_columns'] = sorted(scan['read_columns'] | {'row_id'})
        scans.append(scan)
    plan_checks = []
    reference_targets = []
    for check in normalized:
        entry = dict(check)
        entry['responsibility'] = (
            'denominator = exactly the %s rows matching %s; rows selected by other checks are never '
            'added to it, and its rows are never removed by other checks\' filters'
            % (check['source'], check['population_canonical']))
        plan_checks.append(entry)
        if check['kind'] == 'reference':
            assertion = check['assertion']
            reference_targets.append({
                'check_id': check['check_id'], 'target_source': assertion['target_source'],
                'scan_id': assertion['target_scan_id'], 'target_population': assertion['target_population'],
                'target_population_canonical': assertion['target_population_canonical'],
                'target_fields': assertion['target_fields'],
                'domain': ('distinct typed composite keys (%s) of %s rows matching %s; duplicate target '
                           'keys collapse to one and never amplify source misses; target keys containing '
                           'NULL are excluded'
                           % (','.join(assertion['target_fields']), assertion['target_source'],
                              assertion['target_population_canonical']))})
    return {'version': 1, 'status': 'ok',
            'sources': {name: {'file': spec['file'],
                               'schema': {field: list(types) for field, types in spec['schema'].items()}}
                        for name, spec in sources.items()},
            'scans': scans, 'checks': plan_checks, 'reference_targets': reference_targets,
            'sharing': {'scan_count': len(scans), 'check_count': len(plan_checks),
                        'note': 'one shared scan per involved source; every check keeps its own '
                                'population mask and its own denominator'}}


# --- legacy probe API (same-population normal path preserved) ---

def _mask(predicate):
    return lambda row: matches(row, predicate)


def compile_checks(checks):
    masks = [_mask(check['population']) for check in checks]
    return {'checks': checks, 'masks': masks,
            'accept': lambda row: any(mask(row) for mask in masks)}
