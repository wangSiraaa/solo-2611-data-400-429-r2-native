"""Branch-aware data contracts.

A *shape* describes one definite row shape that can reach a node.  Unlike the
old collapsed wide table, shapes keep mutually exclusive fields apart: two
shapes that each carry one of two fields are never merged into a shape that
claims both fields can appear together.

Every shape remembers its origin (a source node plus a declared variant) and
the guards under which it is reachable, so a rejected plan can be diagnosed
with a concrete source row that reproduces the failure.
"""

INT, TEXT, BOOL, NULL = 'int', 'text', 'bool', 'null'
RUNTIME_TYPES = (INT, TEXT, BOOL, NULL)
ABSTRACT_TYPES = frozenset(RUNTIME_TYPES)
DECL_TYPES = frozenset(RUNTIME_TYPES + ('literal',))
MAX_FIELDS = 16
MAX_VARIANTS = 8


class CompileError(Exception):
    """Static compilation failure.

    ``detail`` is a JSON-serialisable diagnostic carrying the node, expression
    location, originating shape, reachability guards and a concrete
    counterexample row whenever those are known.
    """

    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code = code
        self.detail = {'code': code, 'message': message}
        self.detail.update(detail)


def type_category(value):
    """Runtime type category.  Booleans are NOT integers, true != 1."""
    if value is None:
        return NULL
    if type(value) is bool:
        return BOOL
    if type(value) is int:
        return INT
    if isinstance(value, str):
        return TEXT
    return None


def is_literal_value(value):
    """Literal values are restricted to text, bool, int or NULL."""
    return type_category(value) is not None


class Desc:
    """Field descriptor.

    ``types``  : frozenset of runtime type categories the field may have.
    ``domain`` : None for an open domain, otherwise a frozenset of tagged
                 values ``(category, value)``.  A finite domain marks a
                 discriminant field that ``eq`` guards are allowed to use.
    """

    __slots__ = ('types', 'domain')

    def __init__(self, types, domain=None):
        self.types = frozenset(types)
        self.domain = None if domain is None else frozenset(domain)

    def __eq__(self, other):
        return isinstance(other, Desc) and self.types == other.types and self.domain == other.domain

    def __hash__(self):
        return hash((self.types, self.domain))

    def __repr__(self):
        return 'Desc(types=%r, domain=%r)' % (set(self.types),
                                              None if self.domain is None else set(self.domain))


def open_desc(category):
    return Desc((category,), None)


def lit_desc(value):
    category = type_category(value)
    return Desc((category,), ((category, value),))


def spec_to_desc(spec):
    """Compile a declared field spec into a descriptor."""
    t = spec['type']
    if t == 'literal':
        if 'value' not in spec:
            raise ValueError('literal spec requires a value')
        value = spec['value']
        if not is_literal_value(value):
            raise ValueError('literal value must be text, bool, int or null')
        return lit_desc(value)
    if t in ABSTRACT_TYPES:
        if t == NULL:
            # A null-typed field is a fixed NULL value, i.e. a finite domain.
            return Desc((NULL,), ((NULL, None),))
        return open_desc(t)
    raise ValueError('unknown schema type %r' % (t,))


def desc_to_spec(desc):
    """Serialise a descriptor back to the declaration language.

    Finite domains with more than one value use the output-only ``one_of``
    extension; mixed open types use ``union``.
    """
    if desc.domain is not None:
        values = [value for _, value in sorted(desc.domain, key=lambda tv: (tv[0], repr(tv[1])))]
        if len(values) == 1:
            value = values[0]
            if value is None:
                return {'type': NULL}
            return {'type': 'literal', 'value': value}
        return {'type': 'one_of', 'values': values}
    if len(desc.types) == 1:
        return {'type': next(iter(desc.types))}
    return {'type': 'union', 'of': sorted(desc.types)}


def union_descs(descs):
    types, domain, open_domain = set(), set(), False
    for desc in descs:
        types |= desc.types
        if desc.domain is None:
            open_domain = True
        else:
            domain |= desc.domain
    return Desc(types, None if open_domain else frozenset(domain))


def narrow_type(desc, category):
    types = desc.types & {category}
    domain = None if desc.domain is None else frozenset(
        tv for tv in desc.domain if tv[0] == category)
    return Desc(types, domain)


def remove_type(desc, category):
    types = desc.types - {category}
    domain = None if desc.domain is None else frozenset(
        tv for tv in desc.domain if tv[0] != category)
    return Desc(types, domain)


def narrow_value(desc, category, value):
    return Desc((category,), ((category, value),))


def remove_value(desc, category, value):
    assert desc.domain is not None
    domain = frozenset(tv for tv in desc.domain if tv != (category, value))
    return Desc(frozenset(cat for cat, _ in domain), domain)


class Shape:
    """One definite, branch-correlated row shape."""

    __slots__ = ('fields', 'origin', 'guards')

    def __init__(self, fields, origin, guards=()):
        self.fields = dict(fields)
        # origin: (source_id, variant_index, example_row)
        self.origin = origin
        self.guards = list(guards)

    def replace_fields(self, fields):
        return Shape(fields, self.origin, self.guards)

    def with_field(self, name, desc):
        fields = dict(self.fields)
        fields[name] = desc
        return Shape(fields, self.origin, self.guards)

    def with_guard(self, guard_text):
        return Shape(self.fields, self.origin, self.guards + [guard_text])

    def provenance(self):
        source_id, variant_index, example = self.origin
        return {'source': source_id, 'variant': variant_index,
                'guards': list(self.guards), 'counterexample_row': dict(example)}


def validate_spec(spec):
    if not isinstance(spec, dict) or 'type' not in spec:
        raise ValueError('field spec must be an object with a type')
    t = spec['type']
    if t not in DECL_TYPES:
        raise ValueError('unknown field type %r' % (t,))
    if t == 'literal':
        if 'value' not in spec:
            raise ValueError('literal spec requires a value')
        if not is_literal_value(spec['value']):
            raise ValueError('literal value must be text, bool, int or null')
    extra = set(spec) - {'type', 'value'}
    if extra:
        raise ValueError('unknown spec keys %r' % (sorted(extra),))


def variant_from_decl(variant):
    if not isinstance(variant, dict) or not variant:
        raise ValueError('variant must be a non-empty object of field specs')
    if len(variant) > MAX_FIELDS:
        raise ValueError('variant declares more than %d fields' % MAX_FIELDS)
    fields = {}
    for name, spec in variant.items():
        if not isinstance(name, str) or not name:
            raise ValueError('field names must be non-empty strings')
        validate_spec(spec)
        fields[name] = spec_to_desc(spec)
    return fields


def example_row_for(variant):
    """Build a concrete legal source row for a declared variant."""
    row = {}
    for name, spec in variant.items():
        t = spec['type']
        if t == 'literal':
            row[name] = spec['value']
        elif t == INT:
            row[name] = 1
        elif t == TEXT:
            row[name] = 'a'
        elif t == BOOL:
            row[name] = True
        elif t == NULL:
            row[name] = None
    return row


def accepts(row, variant):
    """Exact runtime matching: field set equality and precise types/values."""
    if not isinstance(row, dict) or set(row) != set(variant):
        return False
    for name, spec in variant.items():
        value = row[name]
        category = type_category(value)
        if category is None:
            return False
        t = spec['type']
        if t == 'literal':
            wanted = spec['value']
            if category != type_category(wanted) or value != wanted:
                return False
        elif t != category:
            return False
    return True


def shape_accepted_by_variant(shape, expected_fields):
    """Static receivability: every possible row of ``shape`` fits the variant.

    ``expected_fields`` maps field names to parsed :class:`Desc` objects.
    """
    if set(shape.fields) != set(expected_fields):
        return False
    for name, target in expected_fields.items():
        desc = shape.fields[name]
        if target.domain is not None:
            # literal / null-typed field: require a subset of fixed values
            if desc.domain is None or not set(desc.domain) <= set(target.domain):
                return False
        else:
            if not set(desc.types) <= set(target.types):
                return False
    return True


def contract_from_shapes(shapes):
    """JSON-serialisable output contract with provenance kept separate."""
    variants, provenance = [], []
    for shape in shapes:
        variants.append({name: desc_to_spec(desc) for name, desc in shape.fields.items()})
        provenance.append(shape.provenance())
    return {'shapes': variants, 'provenance': provenance}
