"""Query planner: JSON AST -> index access plan.

The planner never executes anything and never reads documents.  It chooses
between prefix scans, posting intersections/unions, multiple index branches
and justified full scans.  Crucial semantic rules:

* Bounds that read ``items.*`` slots from a **single scan** are only allowed
  when they come from one ``elemMatch`` block - then they are guaranteed to
  describe one array element.  Independent ``and`` leaves always get separate
  scans whose posting sets are combined at *document* level, so the planner
  can never invent a same-element combination the document does not contain.
* A leaf that no index can serve simply contributes no scan; under ``and``
  the remaining scans still form a valid candidate superset checked
  residually, while an ``or`` with an unservable branch degrades to a full
  scan.
* Prefix discipline: ordered index slots may only be bounded up to the first
  gap.  Leading *root* gaps can be expanded by enumerating the distinct root
  prefix values; a gap on an array slot cannot.
"""

from indexer import (
    IndexSpec,
    TAG_MISSING,
    TAG_NULL,
    TAG_BOOL,
    TAG_INT,
    TAG_TEXT,
    TAG_EMPTY,
    split_path,
)


def leaf_bound(leaf):
    """Translate one leaf into a typed index bound descriptor, or None.

    None means the index cannot serve the leaf (e.g. exists=false on an array
    slot needs per-document aggregation).  The caller still keeps the leaf in
    the residual AST.
    """
    field = leaf['field']
    op = leaf['op']
    value = leaf['value']
    _, sub = split_path(field)
    is_array_slot = sub is not None

    if op == 'exists':
        if not isinstance(value, bool):
            raise ValueError('exists requires a boolean operand')
        if value:
            # Present: any typed value.  On an array slot the whole-array
            # placeholders (missing / empty) are not "present" either.
            excluded = [TAG_MISSING, TAG_EMPTY] if is_array_slot else [TAG_MISSING]
            return {'op': 'tag_not_in', 'exclude_tags': excluded}
        if is_array_slot:
            # A document may have some elements carrying the subfield and
            # others missing it; the index cannot answer without aggregating
            # every posting of the document.
            return None
        return {'op': 'eq', 'tag': TAG_MISSING, 'value': None}

    if op == 'eq':
        if value is None:
            return {'op': 'eq', 'tag': TAG_NULL, 'value': None}
        if isinstance(value, bool):
            return {'op': 'eq', 'tag': TAG_BOOL, 'value': int(value)}
        if isinstance(value, int):
            return {'op': 'eq', 'tag': TAG_INT, 'value': value}
        if isinstance(value, str):
            return {'op': 'eq', 'tag': TAG_TEXT, 'value': value}
        raise ValueError('unsupported eq operand: %r' % (value,))

    if op in ('ge', 'le'):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError('range operand must be an integer, got %r' % (value,))
        return {'op': op, 'tag': TAG_INT, 'value': value}

    raise ValueError('unsupported operator: %r' % (op,))


def _referenced_fields(node, accumulator):
    if 'and' in node:
        for child in node['and']:
            _referenced_fields(child, accumulator)
    elif 'or' in node:
        for child in node['or']:
            _referenced_fields(child, accumulator)
    elif 'elemMatch' in node:
        for leaf in node['elemMatch']:
            accumulator.add(leaf['field'])
    elif 'field' in node:
        accumulator.add(node['field'])
    else:
        raise ValueError('unrecognized query node: %r' % (node,))


def _is_leaf(node):
    return 'field' in node and 'op' in node and 'value' in node


def _feasible_prefix(spec, slot_bounds, correlated):
    """Validate ordered-prefix access for one scan.

    Returns ``(bounds_by_slot, expanded_root_gap_slots)`` where the second
    element lists leading root slots that must be enumerated as distinct
    prefix values at execution time.  Returns ``(None, None)`` when the index
    cannot serve these bounds in one scan: any gap on an array slot is fatal
    (independent leaves then get their own scans instead).
    """
    if not slot_bounds:
        return None, None
    max_slot = max(slot_bounds)
    chosen = {}
    root_gaps = []
    for slot in range(max_slot + 1):
        if slot in slot_bounds:
            chosen[slot] = slot_bounds[slot]
            continue
        # Gap.  Only a leading root slot gap may be expanded, and only while
        # nothing array-bound has been seen yet.
        if (
            not spec.is_array_slot[slot]
            and all(not spec.is_array_slot[earlier] for earlier in range(slot))
        ):
            root_gaps.append(slot)
            continue
        # Array-slot gap (or a root gap behind array slots): this index cannot
        # combine these bounds in one scan without breaking prefix order.
        return None, None
    if not chosen:
        return None, None
    return chosen, root_gaps


def _make_scan(spec, index_name, slot_bounds, correlated, binding):
    chosen, root_gaps = _feasible_prefix(spec, slot_bounds, correlated)
    if chosen is None:
        return None
    return {
        'op': 'scan',
        'index_name': index_name,
        'paths': list(spec.paths),
        'bounds': [
            {
                'slot': slot,
                'field': spec.paths[slot],
                'bound': chosen[slot],
            }
            for slot in sorted(chosen)
        ],
        'expanded_root_gaps': root_gaps,
        'correlated': correlated,
        'binding': binding,
    }


def _plan_leaf(leaf, specs):
    bound = leaf_bound(leaf)
    if bound is None:
        return None
    field = leaf['field']
    for name, spec in specs:
        if field not in spec.paths:
            continue
        slot = spec.paths.index(field)
        scan = _make_scan(spec, name, {slot: bound}, correlated=False,
                          binding='independent_leaf')
        if scan is not None:
            return scan
    return None


def _plan_elem_match(node, specs):
    block = node['elemMatch']
    if not block or not all(_is_leaf(leaf) for leaf in block):
        raise ValueError('elemMatch requires a non-empty list of leaf conditions')
    prefixes = {split_path(leaf['field'])[0] for leaf in block}
    if any(split_path(leaf['field'])[1] is None for leaf in block):
        raise ValueError('elemMatch cannot bind root fields: %r' % (block,))
    if len(prefixes) != 1:
        raise ValueError('elemMatch leaves must share one array prefix: %r' % (block,))
    array_name = next(iter(prefixes))

    # First choice: one correlated scan covering the whole block, so every
    # bound in the scan describes the same array element.
    for name, spec in specs:
        if spec.array_name != array_name:
            continue
        slot_bounds = {}
        for leaf in block:
            if leaf['field'] not in spec.paths:
                slot_bounds = None
                break
            bound = leaf_bound(leaf)
            if bound is None:
                slot_bounds = None
                break
            slot_bounds[spec.paths.index(leaf['field'])] = bound
        if slot_bounds is None:
            continue
        scan = _make_scan(spec, name, slot_bounds, correlated=True,
                          binding='elemMatch')
        if scan is not None:
            return scan, True

    # Fallback: serve individual leaves independently and re-check the whole
    # elemMatch residually.  This must never apply same-element intersection
    # bounds, so each leaf gets its own scan.
    children = [_plan_leaf(leaf, specs) for leaf in block]
    children = [child for child in children if child is not None]
    if not children:
        return None, 'no index can serve elemMatch fields %r; block stays residual' % sorted(
            leaf['field'] for leaf in block)
    if len(children) == 1:
        return children[0], None
    return {'op': 'intersect', 'children': children,
            'reason': 'elemMatch partially covered; correlation enforced residually'}, None


def _dedupe_scans(scans):
    seen = set()
    unique = []
    for scan in scans:
        key = (
            scan['index_name'],
            tuple((item['slot'], repr(item['bound'])) for item in scan['bounds']),
            tuple(scan['expanded_root_gaps']),
        )
        if key not in seen:
            seen.add(key)
            unique.append(scan)
    return unique


def _plan_node(node, specs):
    """Return (plan_subtree_or_None, full_scan_reason_or_None)."""
    if _is_leaf(node):
        scan = _plan_leaf(node, specs)
        if scan is None:
            return None, 'no index can serve leaf %r; condition stays residual' % node['field']
        return scan, None

    if 'elemMatch' in node:
        return _plan_elem_match(node, specs)

    if 'and' in node:
        children = []
        reasons = []
        for child in node['and']:
            subtree, reason = _plan_node(child, specs)
            if subtree is not None:
                children.append(subtree)
            if reason is not None:
                reasons.append(reason)
        if not children:
            return None, 'all AND branches are index-unservable: %s' % '; '.join(reasons)
        if len(children) == 1:
            return children[0], None
        return {'op': 'intersect', 'children': children}, None

    if 'or' in node:
        children = []
        for child in node['or']:
            subtree, _ = _plan_node(child, specs)
            if subtree is None:
                return None, 'OR branch %r has no index access; union would miss documents' % child
            children.append(subtree)
        if len(children) == 1:
            return children[0], None
        return {'op': 'union', 'children': children}, None

    raise ValueError('unrecognized query node: %r' % (node,))


def plan_query(ast, named_specs):
    """Build a full plan.

    ``named_specs`` is a list of ``(name, IndexSpec)`` ordered by preference.
    The residual AST is always the original query: candidate sets are
    supersets, never answers.
    """
    referenced = set()
    _referenced_fields(ast, referenced)
    candidate_indexes = [
        {'name': name, 'paths': list(spec.paths)}
        for name, spec in named_specs
        if any(field in spec.paths for field in referenced)
    ]

    if not named_specs:
        subtree, reason = None, 'no indexes declared'
    else:
        subtree, reason = _plan_node(ast, named_specs)

    plan = {
        'ast': ast,
        'candidate_indexes': candidate_indexes,
        'residual_ast': ast,
    }
    if subtree is None:
        plan['strategy'] = 'full_scan'
        plan['full_scan_reason'] = reason
        plan['tree'] = None
    else:
        plan['strategy'] = 'index'
        plan['full_scan_reason'] = None
        plan['tree'] = subtree
    return plan


def collect_scans(tree):
    if tree is None:
        return []
    if tree['op'] in ('scan',):
        return [tree]
    scans = []
    for child in tree['children']:
        scans.extend(collect_scans(child))
    return _dedupe_scans(scans)
