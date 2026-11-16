"""Random document/query generation for differential (oracle) testing.

All generators are deterministic once a seed is supplied, and they deliberately
exercise the edge cases the spec calls out: missing fields, explicit nulls,
booleans vs integers, empty/missing items arrays, repeated elements and the
same value across documents.
"""

import random

SKUS = ['A', 'B', 'Z', 'sku-x', 'SKU-7', '']
QTY_POOL = [1, 5, 9, 0, -3, 100]
TENANTS = ['t', 'u', '']
WAREHOUSES = ['w1', 'w2', None]
ROOT_BOOLS = [True, False]


def _maybe_omit(rng, probability=0.25):
    return rng.random() < probability


def make_element(rng):
    element = {}
    if not _maybe_omit(rng):
        element['sku'] = rng.choice(SKUS)
    if not _maybe_omit(rng):
        # Occasionally a bool next to ints to check type discrimination.
        element['qty'] = rng.choice(QTY_POOL) if rng.random() < 0.85 else rng.choice([True, False])
    if not _maybe_omit(rng):
        element['warehouse'] = rng.choice(WAREHOUSES)
    return element


def make_document(rng, doc_id):
    document = {'_id': doc_id}
    if not _maybe_omit(rng, 0.1):
        document['tenant'] = rng.choice(TENANTS)
    if not _maybe_omit(rng, 0.15):
        document['enabled'] = rng.choice(ROOT_BOOLS)
    shape = rng.choice(['populated', 'empty', 'missing'])
    if shape == 'populated':
        document['items'] = [make_element(rng) for _ in range(rng.randint(1, 6))]
        # Sometimes repeat an element verbatim to test posting collapse.
        if document['items'] and rng.random() < 0.3:
            document['items'].append(dict(document['items'][rng.randrange(len(document['items']))]))
    elif shape == 'empty':
        document['items'] = []
    # else: items key absent
    return document


def make_documents(seed, count):
    rng = random.Random(seed)
    return [make_document(rng, 'doc-%04d' % i) for i in range(count)]


# --------------------------------------------------------------------- queries
def _leaf(rng, fields):
    field = rng.choice(fields)
    if field == 'items.qty' or field == 'enabled':
        op = rng.choice(['eq', 'ge', 'le', 'exists'])
        if op == 'eq':
            value = rng.choice(QTY_POOL + [True, False, None])
        elif op in ('ge', 'le'):
            value = rng.choice(QTY_POOL)
        else:
            value = rng.choice([True, False])
    elif field == 'items.warehouse':
        op = rng.choice(['eq', 'exists'])
        value = rng.choice(WAREHOUSES) if op == 'eq' else rng.choice([True, False])
    elif field == 'items.sku':
        op = rng.choice(['eq', 'exists'])
        value = rng.choice(SKUS) if op == 'eq' else rng.choice([True, False])
    else:  # tenant
        op = rng.choice(['eq', 'exists'])
        value = rng.choice(TENANTS) if op == 'eq' else rng.choice([True, False])
    node = {'field': field, 'op': op, 'value': value}
    return node


def make_query(rng, depth=0):
    kind = rng.choice(['leaf', 'leaf', 'and', 'or', 'elemMatch']) if depth < 2 else 'leaf'
    if kind == 'leaf':
        return _leaf(rng, ['tenant', 'enabled', 'items.sku', 'items.qty', 'items.warehouse'])
    if kind == 'and':
        return {'and': [make_query(rng, depth + 1) for _ in range(rng.randint(2, 3))]}
    if kind == 'or':
        return {'or': [make_query(rng, depth + 1) for _ in range(rng.randint(2, 3))]}
    # elemMatch: leaves all on items.* sharing the prefix
    block = [
        _leaf(rng, ['items.sku', 'items.qty', 'items.warehouse'])
        for _ in range(rng.randint(1, 3))
    ]
    return {'elemMatch': block}


def make_queries(seed, count):
    rng = random.Random(seed)
    return [make_query(rng) for _ in range(count)]


# Targeted shapes the spec requires explicitly.
def canonical_fixture_documents():
    return [
        {'_id': 'mixed', 'tenant': 't', 'enabled': True,
         'items': [{'sku': 'A', 'qty': 1}, {'sku': 'B', 'qty': 5}]},
        {'_id': 'same', 'tenant': 't', 'enabled': True,
         'items': [{'sku': 'A', 'qty': 5}]},
    ]


def elem_match_a5():
    return {'elemMatch': [
        {'field': 'items.sku', 'op': 'eq', 'value': 'A'},
        {'field': 'items.qty', 'op': 'eq', 'value': 5},
    ]}


def plain_and_a5():
    return {'and': [
        {'field': 'items.sku', 'op': 'eq', 'value': 'A'},
        {'field': 'items.qty', 'op': 'eq', 'value': 5},
    ]}


def benchmark_documents():
    documents = []
    for number in range(500):
        documents.append({
            '_id': 'd%03d' % number,
            'tenant': 't',
            'enabled': True,
            'items': [{'sku': 'wanted' if number < 10 else 'other',
                       'qty': number, 'warehouse': None}],
        })
    return documents
