"""Independent per-document / per-element query interpreter.

This module never imports the indexer or the planner and never touches the
on-disk index.  It walks raw documents, so it is used both as the correctness
oracle in the tests and as the residual filter after index candidate
generation.

Type discipline (mirrors the index type tags):

* ``missing`` (field absent) and explicit ``null`` are different.
* booleans and integers never compare equal (``True != 1``).
* ``eq null`` matches explicit null only; ordinary comparisons never match
  missing or null.
* ``ge`` / ``le`` apply to integers only; every other stored type fails.
* ``exists`` tests presence, and an explicit null counts as present.
"""

# Sentinel for a field that is absent from a document or array element.
MISSING = object()


def _split_path(field):
    pieces = field.split('.')
    if len(pieces) == 1:
        return pieces[0], None
    if len(pieces) == 2:
        return pieces[0], pieces[1]
    raise ValueError('unsupported field path: %r' % field)


def _strict_equal(stored, wanted):
    """Equality that keeps JSON types distinct (bool is not int, null is its own)."""
    if wanted is None:
        return stored is None
    if isinstance(wanted, bool):
        return isinstance(stored, bool) and stored == wanted
    if isinstance(wanted, int):
        return isinstance(stored, int) and not isinstance(stored, bool) and stored == wanted
    if isinstance(wanted, str):
        return isinstance(stored, str) and stored == wanted
    raise ValueError('unsupported eq operand: %r' % (wanted,))


def leaf_on_value(leaf, value):
    """Evaluate one leaf condition against a single scalar slot value.

    ``value`` may be :data:`MISSING`, ``None`` (explicit JSON null), a bool,
    an int or a str.
    """
    op = leaf['op']
    wanted = leaf.get('value', MISSING)

    if op == 'exists':
        if not isinstance(wanted, bool):
            raise ValueError('exists requires a boolean operand')
        return (value is not MISSING) == wanted

    # Every other operator leaves missing unmatched.
    if value is MISSING:
        return False

    if op == 'eq':
        return _strict_equal(value, wanted)

    if op in ('ge', 'le'):
        if not isinstance(wanted, int) or isinstance(wanted, bool):
            raise ValueError('range operand must be an integer, got %r' % (wanted,))
        # Ranges are defined for integer stored values only.
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        return value >= wanted if op == 'ge' else value <= wanted

    raise ValueError('unsupported operator: %r' % (op,))


def _eval_leaf_on_document(leaf, doc):
    array_name, sub = _split_path(leaf['field'])
    if sub is None:
        value = doc[array_name] if array_name in doc else MISSING
        return leaf_on_value(leaf, value)

    array = doc.get(array_name, MISSING)
    if array is MISSING or not isinstance(array, list):
        # Missing/empty array: no element can carry the value.  exists=false
        # still has to be answered from presence, handled explicitly below.
        if leaf['op'] == 'exists':
            return leaf_on_value(leaf, MISSING)
        return False

    if leaf['op'] == 'exists':
        present = any(sub in element for element in array if isinstance(element, dict))
        return present == bool(leaf['value'])

    return any(
        leaf_on_value(leaf, element[sub] if sub in element else MISSING)
        for element in array
        if isinstance(element, dict)
    )


def _eval_elem_match(block, doc):
    # Root fields do not participate in element binding.
    for leaf in block:
        _, sub = _split_path(leaf['field'])
        if sub is None:
            raise ValueError('elemMatch cannot bind root field %r' % leaf['field'])

    array_name = block[0]['field'].split('.')[0]
    array = doc.get(array_name, MISSING)
    if array is MISSING or not isinstance(array, list):
        return False
    return any(
        all(
            leaf_on_value(
                leaf,
                element[leaf['field'].split('.', 1)[1]]
                if leaf['field'].split('.', 1)[1] in element
                else MISSING,
            )
            for leaf in block
        )
        for element in array
        if isinstance(element, dict)
    )


def evaluate(node, doc):
    """Evaluate a query AST node against one document. Returns a boolean."""
    if 'and' in node:
        return all(evaluate(child, doc) for child in node['and'])
    if 'or' in node:
        return any(evaluate(child, doc) for child in node['or'])
    if 'elemMatch' in node:
        block = node['elemMatch']
        if not block or not all(set(leaf) == {'field', 'op', 'value'} for leaf in block):
            raise ValueError('elemMatch requires a non-empty list of leaf conditions')
        return _eval_elem_match(block, doc)
    if 'field' in node and 'op' in node:
        return _eval_leaf_on_document(node, doc)
    raise ValueError('unrecognized query node: %r' % (node,))


def query_documents(ast, documents):
    """Oracle entry point: return matching document ids in codepoint order."""
    return sorted(
        document['_id']
        for document in documents
        if evaluate(ast, document)
    )
