"""Branch-aware expression language: static analysis and runtime evaluation.

Value expressions: field, const, add, text_concat, if, get.
Guard expressions: type_is, has, eq, and, or, not.

Static analysis is performed against a concrete :class:`~contracts.Shape`
(one definite row shape).  Guard refinement can prove branches unreachable,
and unreachable expressions are never analysed, so mutually exclusive
branches cannot cause false rejections.
"""

from contracts import (CompileError, ABSTRACT_TYPES,
                       type_category, narrow_type, remove_type,
                       narrow_value, remove_value, lit_desc, open_desc,
                       union_descs)

VALUE_OPS = {'add', 'text_concat', 'if', 'get'}
GUARD_OPS = {'type_is', 'has', 'eq', 'and', 'or', 'not'}
_EXPR_KEYS = frozenset(('field', 'const', 'op', 'args'))

TRUE, FALSE, UNKNOWN = True, False, None


def expr_text(expr):
    """Stable textual rendering used in reachability-guard diagnostics."""
    if 'field' in expr:
        return 'field(%s)' % expr['field']
    if 'const' in expr:
        return 'const(%r)' % (expr['const'],)
    return '%s(%s)' % (expr['op'], ', '.join(expr_text(a) for a in expr['args']))


# --------------------------------------------------------------------------- #
# Structural validation
# --------------------------------------------------------------------------- #

def _validate(expr, guard):
    if not isinstance(expr, dict) or not expr:
        raise ValueError('expression must be a non-empty object')
    extra = set(expr) - _EXPR_KEYS
    if extra:
        raise ValueError('unknown expression keys %r' % (sorted(extra),))
    if 'field' in expr or 'const' in expr:
        if 'op' in expr or 'args' in expr:
            raise ValueError('field/const expressions carry no op/args')
        if 'field' in expr and (not isinstance(expr['field'], str) or not expr['field']):
            raise ValueError('field name must be a non-empty string')
        if 'const' in expr and type_category(expr['const']) is None:
            raise ValueError('const must be text, bool, int or null')
        return
    op = expr.get('op')
    args = expr.get('args')
    allowed = GUARD_OPS if guard else VALUE_OPS
    if op not in allowed:
        raise ValueError('op %r is not allowed here' % (op,))
    if not isinstance(args, list) or not args:
        raise ValueError('op %r requires a non-empty args list' % (op,))
    if op == 'if' and len(args) != 3:
        raise ValueError('if requires exactly 3 args')
    if op == 'get' and (len(args) != 2 or 'field' not in args[0]):
        raise ValueError('get requires args [field, default]')
    if op in ('and', 'or') and len(args) < 2:
        raise ValueError('%s requires at least 2 args' % op)
    if op == 'not' and len(args) != 1:
        raise ValueError('not requires exactly 1 arg')
    if op == 'type_is' and (len(args) != 2 or 'field' not in args[0]
                            or 'const' not in args[1]
                            or args[1]['const'] not in ABSTRACT_TYPES):
        raise ValueError('type_is requires args [field, type-literal]')
    if op == 'has' and (len(args) != 1 or 'field' not in args[0]):
        raise ValueError('has requires one field arg')
    if op == 'eq' and (len(args) != 2 or 'field' not in args[0] or 'const' not in args[1]):
        raise ValueError('eq requires args [field, literal]')
    if op == 'if':
        _validate(args[0], True)
        _validate(args[1], guard)
        _validate(args[2], guard)
        return
    for arg in args:
        _validate(arg, guard)


def validate_value(expr):
    _validate(expr, False)


def validate_guard(expr):
    _validate(expr, True)


# --------------------------------------------------------------------------- #
# Guard evaluation: three-valued satisfaction and shape refinement
# --------------------------------------------------------------------------- #

def _sat(expr, shape):
    """Definite TRUE / FALSE, or UNKNOWN when the guard cannot be decided."""
    op = expr['op']
    if op == 'has':
        return TRUE if expr['args'][0]['field'] in shape.fields else FALSE
    if op == 'type_is':
        name = expr['args'][0]['field']
        wanted = expr['args'][1]['const']
        desc = shape.fields.get(name)
        if desc is None:
            return FALSE
        if desc.types <= {wanted}:
            return TRUE
        if wanted not in desc.types:
            return FALSE
        return UNKNOWN
    if op == 'eq':
        name = expr['args'][0]['field']
        literal = expr['args'][1]['const']
        desc = shape.fields.get(name)
        if desc is None:
            return FALSE
        if desc.domain is None:
            raise CompileError(
                'unsupported_guard',
                'eq needs a finite-literal discriminant field; %r has an open domain' % name,
                guard=expr, field=name, **_prov(shape))
        tag = (type_category(literal), literal)
        if tag not in desc.domain:
            return FALSE
        if desc.domain == frozenset((tag,)):
            return TRUE
        return UNKNOWN
    if op == 'not':
        value = _sat(expr['args'][0], shape)
        return UNKNOWN if value is UNKNOWN else not value
    left, right = expr['args'][0], expr['args'][1:]
    if op == 'and':
        first = _sat(left, shape)
        if first is FALSE:
            return FALSE
        result = first
        for arg in right:
            value = _sat(arg, shape)
            if value is FALSE:
                return FALSE
            if value is UNKNOWN:
                result = UNKNOWN
        return result
    # or
    first = _sat(left, shape)
    if first is TRUE:
        return TRUE
    result = first
    for arg in right:
        value = _sat(arg, shape)
        if value is TRUE:
            return TRUE
        if value is UNKNOWN:
            result = UNKNOWN
    return result


def _prov(shape):
    p = shape.provenance()
    return {'origin': {k: p[k] for k in ('source', 'variant', 'counterexample_row')},
            'reachable_guards': p['guards']}


def _refine1(expr, shape, value):
    """Assume guard ``expr`` has truth value ``value``; return 0+ shapes."""
    op = expr['op']
    if op == 'has':
        name = expr['args'][0]['field']
        present = name in shape.fields
        if value == present:
            return [shape]
        return []
    if op == 'type_is':
        name = expr['args'][0]['field']
        wanted = expr['args'][1]['const']
        desc = shape.fields.get(name)
        if desc is None:
            return [] if value else [shape]
        if value:
            if wanted not in desc.types:
                return []
            return [shape.with_field(name, narrow_type(desc, wanted))]
        if wanted not in desc.types:
            return [shape]
        narrowed = remove_type(desc, wanted)
        if not narrowed.types:
            return []
        return [shape.with_field(name, narrowed)]
    if op == 'eq':
        name = expr['args'][0]['field']
        literal = expr['args'][1]['const']
        desc = shape.fields.get(name)
        if desc is None:
            return [] if value else [shape]
        if desc.domain is None:
            raise CompileError(
                'unsupported_guard',
                'eq needs a finite-literal discriminant field; %r has an open domain' % name,
                guard=expr, field=name, **_prov(shape))
        tag = (type_category(literal), literal)
        if value:
            if tag not in desc.domain:
                return []
            return [shape.with_field(name, narrow_value(desc, *tag))]
        if tag not in desc.domain:
            return [shape]
        narrowed = remove_value(desc, *tag)
        if not narrowed.types:
            return []
        return [shape.with_field(name, narrowed)]
    if op == 'not':
        return _refine1(expr['args'][0], shape, not value)
    left, rest = expr['args'][0], expr['args'][1:]
    if op == 'and':
        if value:
            shapes = [shape]
            for arg in expr['args']:
                shapes = _refine_many(arg, shapes, True)
            return shapes
        # not (a and b ...): left false, or left true and the rest false
        out = _refine1(left, shape, False)
        true_path = _refine1(left, shape, True)
        if rest:
            tail = {'op': 'and', 'args': rest} if len(rest) > 1 else rest[0]
            for sh in true_path:
                out.extend(_refine1(tail, sh, False))
        return out
    # or
    if not value:
        shapes = [shape]
        for arg in expr['args']:
            shapes = _refine_many(arg, shapes, False)
        return shapes
    out = _refine1(left, shape, True)
    false_path = _refine1(left, shape, False)
    if rest:
        tail = {'op': 'or', 'args': rest} if len(rest) > 1 else rest[0]
        for sh in false_path:
            out.extend(_refine1(tail, sh, True))
    return out


def _refine_many(expr, shapes, value):
    out = []
    for shape in shapes:
        out.extend(_refine1(expr, shape, value))
    return out


def refine_guard(expr, shapes, value):
    """Public entry: refine shapes under ``expr`` being true/false.

    The assumed guard text is attached to every surviving shape so later
    diagnostics can explain why the path is reachable.
    """
    text = expr_text(expr) if value else 'not(%s)' % expr_text(expr)
    return [s.with_guard(text) for s in _refine_many(expr, shapes, value)]


def guard_sat(expr, shape):
    return _sat(expr, shape)


# --------------------------------------------------------------------------- #
# Static analysis of value expressions against one definite shape
# --------------------------------------------------------------------------- #

def _fail(code, message, expr, shape, node_id, path, **extra):
    detail = {'node': node_id, 'expr_path': path, 'expr': expr}
    detail.update(_prov(shape))
    detail.update(extra)
    raise CompileError(code, message, **detail)


def analyze(expr, shape, node_id, path='expr'):
    """Return the :class:`Desc` of ``expr`` under ``shape`` or raise."""
    if 'field' in expr:
        name = expr['field']
        desc = shape.fields.get(name)
        if desc is None:
            _fail('missing_field',
                  'field %r is absent on this reachable shape' % name,
                  expr, shape, node_id, path)
        return desc
    if 'const' in expr:
        return lit_desc(expr['const'])
    op, args = expr['op'], expr['args']
    if op == 'get':
        name = args[0]['field']
        if name in shape.fields:
            # Present (even when NULL): the default is never used and is not
            # analysed, so an unreachable default cannot cause a false reject.
            return shape.fields[name]
        sub_path = '%s.args[1]' % path
        return analyze(args[1], shape, node_id, sub_path)
    if op == 'if':
        guard, then_expr, else_expr = args
        true_shapes = refine_guard(guard, [shape], True)
        false_shapes = refine_guard(guard, [shape], False)
        descs = []
        for sh in true_shapes:
            descs.append(analyze(then_expr, sh, node_id, '%s.args[1]' % path))
        for sh in false_shapes:
            descs.append(analyze(else_expr, sh, node_id, '%s.args[2]' % path))
        if not descs:
            _fail('unreachable_expr', 'if has no reachable branch',
                  expr, shape, node_id, path)
        return union_descs(descs)
    if op == 'add':
        descs = []
        for index, arg in enumerate(args):
            desc = analyze(arg, shape, node_id, '%s.args[%d]' % (path, index))
            if desc.types != {'int'}:
                _fail('type_error', 'add requires mathematical integers, got %s'
                      % sorted(desc.types), expr, shape, node_id, path,
                      actual_types=sorted(desc.types), expected=['int'])
            descs.append(desc)
        return open_desc('int')
    if op == 'text_concat':
        for index, arg in enumerate(args):
            desc = analyze(arg, shape, node_id, '%s.args[%d]' % (path, index))
            if desc.types != {'text'}:
                _fail('type_error', 'text_concat requires text, got %s'
                      % sorted(desc.types), expr, shape, node_id, path,
                      actual_types=sorted(desc.types), expected=['text'])
        return open_desc('text')
    _fail('invalid_decl', 'unknown value op %r' % op, expr, shape, node_id, path)


def analyze_where_guard(expr, shapes, node_id):
    """Validate a where guard against every still-reachable shape."""
    for shape in shapes:
        validate_guard(expr)
        # Force the eq discriminant check on this shape.
        _sat(expr, shape)
    return refine_guard(expr, shapes, True)


# --------------------------------------------------------------------------- #
# Runtime evaluation (only reached after successful compilation)
# --------------------------------------------------------------------------- #

_MISSING = object()


def evaluate(expr, row):
    if 'field' in expr:
        return row[expr['field']]
    if 'const' in expr:
        return expr['const']
    op, args = expr['op'], expr['args']
    if op == 'get':
        value = row.get(args[0]['field'], _MISSING)
        if value is _MISSING:
            return evaluate(args[1], row)
        return value
    if op == 'if':
        branch = args[1] if evaluate_guard(args[0], row) else args[2]
        return evaluate(branch, row)
    if op == 'add':
        total = 0
        for arg in args:
            value = evaluate(arg, row)
            if type(value) is not int:
                raise TypeError('add requires integer, got %r' % (value,))
            total += value
        return total
    if op == 'text_concat':
        parts = []
        for arg in args:
            value = evaluate(arg, row)
            if not isinstance(value, str):
                raise TypeError('text_concat requires text, got %r' % (value,))
            parts.append(value)
        return ''.join(parts)
    raise ValueError('unknown value op %r' % (op,))


def evaluate_guard(expr, row):
    op = expr['op']
    if op == 'has':
        return expr['args'][0]['field'] in row
    if op == 'type_is':
        name = expr['args'][0]['field']
        if name not in row:
            return False
        return type_category(row[name]) == expr['args'][1]['const']
    if op == 'eq':
        name = expr['args'][0]['field']
        if name not in row:
            return False
        value, literal = row[name], expr['args'][1]['const']
        return type_category(value) == type_category(literal) and value == literal
    if op == 'not':
        return not evaluate_guard(expr['args'][0], row)
    if op == 'and':
        for arg in expr['args']:
            if not evaluate_guard(arg, row):
                return False
        return True
    if op == 'or':
        for arg in expr['args']:
            if evaluate_guard(arg, row):
                return True
        return False
    raise ValueError('unknown guard op %r' % (op,))
