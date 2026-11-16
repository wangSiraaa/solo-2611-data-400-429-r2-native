"""Branch-aware static contract compiler and runtime entry point.

Compile flow
------------
1. Structural plan validation: illegal declarations, unknown references and
   cycles are reported before any data is touched.
2. Nodes are processed in topological order.  Each node carries a list of
   :class:`~contracts.Shape` objects -- one per reachable row shape, keeping
   mutually exclusive branches apart.
3. ``assert_contract`` is checked against every reachable shape, not just the
   rows of a particular dataset.

Run flow
-------
The plan is compiled first; a failed compile never starts data processing.
Real source rows are validated against the declared variants (source and
1-based row number are recorded on mismatch), then nodes execute with plain
multiset semantics: concat does not deduplicate and a shared node's rows are
never consumed away.
"""

import argparse
import json
from pathlib import Path

from contracts import (CompileError, MAX_FIELDS, MAX_VARIANTS, Shape,
                       accepts, contract_from_shapes, example_row_for,
                       shape_accepted_by_variant, variant_from_decl)
from expressions import (analyze, analyze_where_guard, evaluate,
                         evaluate_guard, validate_guard, validate_value)

MAX_NODES = 24
NODE_OPS = frozenset(('source', 'where', 'project', 'concat', 'assert_contract'))


# --------------------------------------------------------------------------- #
# Plan-level validation
# --------------------------------------------------------------------------- #

def _invalid(message, **detail):
    code = detail.pop('code', 'invalid_plan')
    raise CompileError(code, message, **detail)


def _require_id(node, seen):
    if not isinstance(node, dict) or 'id' not in node or 'op' not in node:
        _invalid('every node requires string id and op')
    node_id = node['id']
    if not isinstance(node_id, str) or not node_id:
        _invalid('node id must be a non-empty string')
    if node_id in seen:
        _invalid('duplicate node id %r' % node_id, node_id=node_id)
    if node['op'] not in NODE_OPS:
        _invalid('unknown node op %r' % node['op'], node_id=node_id)
    seen.add(node_id)


def _check_name(name, where):
    if not isinstance(name, str) or not name or len(name) > MAX_FIELDS:
        _invalid('%s names must be strings of length 1..%d' % (where, MAX_FIELDS))


def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('nodes'), list) or not plan['nodes']:
        _invalid('plan requires a non-empty nodes list')
    if len(plan['nodes']) > MAX_NODES:
        _invalid('plan exceeds the %d-node limit' % MAX_NODES)
    if 'output' not in plan or not isinstance(plan['output'], str):
        _invalid('plan requires a string output id')
    ids, nodes = set(), plan['nodes']
    for node in nodes:
        _require_id(node, ids)
    by_id = {node['id']: node for node in nodes}
    if plan['output'] not in by_id:
        _invalid('output id %r is not a node' % plan['output'], node_id=plan['output'])

    edges = []
    for node in nodes:
        op, node_id = node['op'], node['id']
        allowed = {'id', 'op'}
        if op == 'source':
            allowed |= {'variants'}
            variants = node.get('variants')
            if not isinstance(variants, list) or not (1 <= len(variants) <= MAX_VARIANTS):
                _invalid('source %r must declare 1..%d variants' % (node_id, MAX_VARIANTS),
                         node_id=node_id)
            names = set()
            for variant in variants:
                try:
                    fields = variant_from_decl(variant)
                except ValueError as exc:
                    _invalid('source %r: %s' % (node_id, exc), node_id=node_id)
                if len(fields) > MAX_FIELDS:
                    _invalid('source %r variant has more than %d fields' % (node_id, MAX_FIELDS),
                             node_id=node_id)
                names |= set(fields)
            if len(names) > MAX_FIELDS:
                _invalid('source %r declares more than %d distinct field names'
                         % (node_id, MAX_FIELDS), node_id=node_id)
        elif op == 'where':
            allowed |= {'input', 'guard'}
            ref = node.get('input')
            if not isinstance(ref, str):
                _invalid('where %r requires a string input' % node_id, node_id=node_id)
            edges.append((ref, node_id))
            try:
                validate_guard(node.get('guard'))
            except ValueError as exc:
                _invalid('where %r: %s' % (node_id, exc), node_id=node_id)
        elif op == 'project':
            allowed |= {'input', 'columns'}
            ref = node.get('input')
            if not isinstance(ref, str):
                _invalid('project %r requires a string input' % node_id, node_id=node_id)
            edges.append((ref, node_id))
            columns = node.get('columns')
            if not isinstance(columns, dict) or not columns:
                _invalid('project %r requires a non-empty columns object' % node_id,
                         node_id=node_id)
            if len(columns) > MAX_FIELDS:
                _invalid('project %r produces more than %d fields' % (node_id, MAX_FIELDS),
                         node_id=node_id)
            for name, expr in columns.items():
                _check_name(name, 'project field')
                try:
                    validate_value(expr)
                except ValueError as exc:
                    _invalid('project %r column %r: %s' % (node_id, name, exc),
                             node_id=node_id, column=name)
        elif op == 'concat':
            allowed |= {'inputs'}
            refs = node.get('inputs')
            if not isinstance(refs, list) or not refs:
                _invalid('concat %r requires a non-empty inputs list' % node_id, node_id=node_id)
            for ref in refs:
                if not isinstance(ref, str):
                    _invalid('concat %r inputs must be string ids' % node_id, node_id=node_id)
                edges.append((ref, node_id))
        else:  # assert_contract
            allowed |= {'input', 'variants'}
            ref = node.get('input')
            if not isinstance(ref, str):
                _invalid('assert_contract %r requires a string input' % node_id, node_id=node_id)
            edges.append((ref, node_id))
            variants = node.get('variants')
            if not isinstance(variants, list) or not variants:
                _invalid('assert_contract %r requires expected variants' % node_id,
                         node_id=node_id)
            for variant in variants:
                try:
                    variant_from_decl(variant)
                except ValueError as exc:
                    _invalid('assert_contract %r: %s' % (node_id, exc), node_id=node_id)
        extra = set(node) - allowed
        if extra:
            _invalid('node %r has unknown keys %r' % (node_id, sorted(extra)), node_id=node_id)

    for src, dst in edges:
        if src not in by_id:
            raise CompileError('unknown_reference',
                               'node %r references unknown node %r' % (dst, src),
                               node=dst, unknown_reference=src)
    order = _topo_order(nodes, edges, by_id)
    return by_id, order


def _topo_order(nodes, edges, by_id):
    indegree = {node_id: 0 for node_id in by_id}
    downstream = {node_id: [] for node_id in by_id}
    for src, dst in edges:
        indegree[dst] += 1
        downstream[src].append(dst)
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    order = []
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for dst in downstream[node_id]:
            indegree[dst] -= 1
            if indegree[dst] == 0:
                ready.append(dst)
        ready.sort()
    if len(order) != len(by_id):
        cycle = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        raise CompileError('cycle', 'plan contains a cycle through %r' % cycle,
                           cycle_nodes=cycle)
    return order


# --------------------------------------------------------------------------- #
# Compilation
# --------------------------------------------------------------------------- #

def _compile(plan):
    by_id, order = validate_plan(plan)
    shapes_by_node = {}
    record = {}

    for node_id in order:
        node = by_id[node_id]
        op = node['op']
        if op == 'source':
            shapes = []
            for index, variant in enumerate(node['variants']):
                fields = variant_from_decl(variant)
                example = example_row_for(variant)
                shapes.append(Shape(fields, (node_id, index, example)))
        elif op == 'where':
            incoming = shapes_by_node[node['input']]
            shapes = analyze_where_guard(node['guard'], incoming, node_id)
        elif op == 'project':
            incoming = shapes_by_node[node['input']]
            shapes = []
            for shape in incoming:
                fields = {}
                for name, expr in node['columns'].items():
                    fields[name] = analyze(expr, shape, node_id)
                shapes.append(shape.replace_fields(fields))
        elif op == 'concat':
            shapes = []
            for ref in node['inputs']:
                # Shapes are kept per input branch; no widening into a union.
                shapes.extend(list(shapes_by_node[ref]))
        else:  # assert_contract
            incoming = shapes_by_node[node['input']]
            expected = [variant_from_decl(v) for v in node['variants']]
            for shape in incoming:
                if not any(shape_accepted_by_variant(shape, variant)
                           for variant in expected):
                    provenance = shape.provenance()
                    raise CompileError(
                        'assert_failed',
                        'assert_contract %r: a reachable output shape is not '
                        'accepted by any expected variant' % node_id,
                        node=node_id, offending_shape=provenance,
                        counterexample_row=provenance['counterexample_row'],
                        expected_variants=node['variants'])
            shapes = list(incoming)

        shapes_by_node[node_id] = shapes
        entry = {'op': op, 'shape_count': len(shapes),
                 'contract': contract_from_shapes(shapes)}
        if op == 'assert_contract':
            entry['asserted_variants'] = node['variants']
        record[node_id] = entry

    output_id = plan['output']
    return {
        'output': output_id,
        'output_contract': record[output_id]['contract'],
        'nodes': record,
        'order': order,
        '_by_id': by_id,
    }


def compile_plan(plan):
    """Compile the plan; raise :class:`CompileError` on any unsafe plan."""
    return _compile(plan)


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #

class DataError(Exception):
    """Real source data violates a declared source contract."""

    def __init__(self, violations):
        super().__init__('source data violates declared variants: %s'
                         % json.dumps(violations, ensure_ascii=False))
        self.violations = violations


def validate_source_rows(node, rows):
    if not isinstance(rows, list):
        raise DataError([{'source': node['id'], 'row': None,
                          'reason': 'source payload must be a JSON array'}])
    violations = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not any(
                accepts(row, variant) for variant in node['variants']):
            violations.append({'source': node['id'], 'row_number': index + 1,
                               'row': row})
    return violations


def run_plan(plan, sources):
    """Compile first, validate real rows, then execute with multiset semantics."""
    compiled = _compile(plan)  # failure raises before data processing
    by_id = compiled['_by_id']
    order = compiled['order']

    violations = []
    for node_id in order:
        node = by_id[node_id]
        if node['op'] == 'source':
            if node_id not in sources:
                raise DataError([{'source': node_id, 'row': None,
                                  'reason': 'no data supplied for source'}])
            violations.extend(validate_source_rows(node, sources[node_id]))
    if violations:
        raise DataError(violations)

    values = {}
    for node_id in order:
        node = by_id[node_id]
        op = node['op']
        if op == 'source':
            values[node_id] = [dict(row) for row in sources[node_id]]
        elif op == 'where':
            guard = node['guard']
            values[node_id] = [row for row in values[node['input']]
                               if evaluate_guard(guard, row)]
        elif op == 'project':
            columns = node['columns']
            values[node_id] = [
                {name: evaluate(expr, row) for name, expr in columns.items()}
                for row in values[node['input']]]
        elif op == 'concat':
            merged = []
            for ref in node['inputs']:
                merged.extend(values[ref])  # no deduplication
            values[node_id] = merged
        else:  # assert_contract: compile already proved safety; rows pass through
            values[node_id] = list(values[node['input']])
    return values[plan['output']]


# --------------------------------------------------------------------------- #
# File-based entry point: contract, diagnostics and data stay separate
# --------------------------------------------------------------------------- #

def execute_to_files(plan_path, rows_path, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    diagnostics_path = out / 'diagnostics.json'
    contract_path = out / 'contract.json'
    data_path = out / 'data.json'
    for stale in (diagnostics_path, contract_path, data_path):
        if stale.exists():
            stale.unlink()

    plan = json.loads(Path(plan_path).read_text())
    diagnostics = {'plan': str(plan_path), 'status': 'ok'}
    try:
        compiled = compile_plan(plan)
    except CompileError as exc:
        diagnostics['status'] = 'compile_failed'
        diagnostics['error'] = exc.detail
        diagnostics_path.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2))
        return diagnostics

    contract_path.write_text(
        json.dumps(compiled['output_contract'], ensure_ascii=False, indent=2))
    diagnostics['compiled_nodes'] = compiled['order']

    if rows_path is not None:
        sources = json.loads(Path(rows_path).read_text())
        try:
            rows = run_plan(plan, sources)
        except DataError as exc:
            diagnostics['status'] = 'data_rejected'
            diagnostics['violations'] = exc.violations
            diagnostics_path.write_text(
                json.dumps(diagnostics, ensure_ascii=False, indent=2))
            return diagnostics
        data_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        diagnostics['output_rows'] = len(rows)

    diagnostics_path.write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2))
    return diagnostics


def main(argv=None):
    parser = argparse.ArgumentParser(description='Branch-aware contract compiler')
    parser.add_argument('plan')
    parser.add_argument('--rows')
    parser.add_argument('--out-dir', default='out')
    args = parser.parse_args(argv)
    diagnostics = execute_to_files(args.plan, args.rows, args.out_dir)
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    return 0 if diagnostics['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
