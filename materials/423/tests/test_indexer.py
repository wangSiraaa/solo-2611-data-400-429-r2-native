"""Indexer unit tests: typed slots, element correlation, placeholders."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indexer import (  # noqa: E402
    IndexSpec,
    TAG_BOOL,
    TAG_EMPTY,
    TAG_INT,
    TAG_MISSING,
    TAG_NULL,
    TAG_TEXT,
    extract_keys,
    validate_document,
)


def pairs(typed_key):
    return list(typed_key)


class IndexSpecTests(unittest.TestCase):
    def test_cross_array_index_rejected(self):
        with self.assertRaises(ValueError):
            IndexSpec(['items.sku', 'other.kind'])

    def test_bad_shapes_rejected(self):
        with self.assertRaises(ValueError):
            IndexSpec([])
        with self.assertRaises(ValueError):
            IndexSpec(['a'] * 5)
        with self.assertRaises(ValueError):
            IndexSpec(['a.b.c'])

    def test_scalar_only_index_accepted(self):
        spec = IndexSpec(['tenant', 'enabled'])
        self.assertIsNone(spec.array_name)


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.spec = IndexSpec(['tenant', 'items.sku', 'items.qty'])

    def test_no_cartesian_combination(self):
        doc = {'_id': 'mixed', 'tenant': 't',
               'items': [{'sku': 'A', 'qty': 1}, {'sku': 'B', 'qty': 5}]}
        keys = extract_keys(doc, self.spec)
        values = [((k[1][1]), (k[2][1])) for k in keys]
        self.assertEqual(sorted(values), [('A', 1), ('B', 5)])
        self.assertNotIn(('A', 5), values)
        self.assertNotIn(('B', 1), values)

    def test_same_element_pair_kept(self):
        doc = {'_id': 'same', 'tenant': 't',
               'items': [{'sku': 'A', 'qty': 5}]}
        keys = extract_keys(doc, self.spec)
        self.assertEqual(len(keys), 1)
        self.assertEqual(pairs(keys[0]),
                         [(TAG_TEXT, 't'), (TAG_TEXT, 'A'), (TAG_INT, 5)])

    def test_missing_subfield_is_tagged_not_dropped(self):
        doc = {'_id': 'd', 'tenant': 't', 'items': [{'sku': 'A'}]}
        key = extract_keys(doc, self.spec)[0]
        self.assertEqual(pairs(key)[2], (TAG_MISSING, None))

    def test_null_distinct_from_missing(self):
        doc_null = {'_id': 'd1', 'tenant': 't',
                    'items': [{'sku': 'A', 'qty': None}]}
        doc_missing = {'_id': 'd2', 'tenant': 't',
                       'items': [{'sku': 'A'}]}
        null_key = extract_keys(doc_null, self.spec)[0]
        missing_key = extract_keys(doc_missing, self.spec)[0]
        self.assertEqual(pairs(null_key)[2], (TAG_NULL, None))
        self.assertEqual(pairs(missing_key)[2], (TAG_MISSING, None))
        self.assertNotEqual(null_key, missing_key)

    def test_bool_distinct_from_integer(self):
        spec = IndexSpec(['enabled'])
        bool_doc = {'_id': 'b', 'enabled': True}
        int_doc = {'_id': 'i', 'enabled': 1}
        self.assertEqual(pairs(extract_keys(bool_doc, spec)[0])[0], (TAG_BOOL, 1))
        self.assertEqual(pairs(extract_keys(int_doc, spec)[0])[0], (TAG_INT, 1))

    def test_empty_array_placeholder(self):
        doc = {'_id': 'e', 'tenant': 't', 'items': []}
        key = extract_keys(doc, self.spec)[0]
        self.assertEqual(pairs(key)[1], (TAG_EMPTY, None))
        self.assertEqual(pairs(key)[2], (TAG_EMPTY, None))

    def test_missing_array_placeholder(self):
        doc = {'_id': 'm', 'tenant': 't'}
        key = extract_keys(doc, self.spec)[0]
        self.assertEqual(pairs(key)[1], (TAG_MISSING, None))
        self.assertEqual(pairs(key)[2], (TAG_MISSING, None))
        self.assertNotEqual(pairs(key)[1][0], TAG_EMPTY)

    def test_duplicate_elements_collapse_to_one_posting(self):
        doc = {'_id': 'dup', 'tenant': 't',
               'items': [{'sku': 'A', 'qty': 5}, {'sku': 'A', 'qty': 5}]}
        self.assertEqual(len(extract_keys(doc, self.spec)), 1)

    def test_scalar_only_one_key_per_document(self):
        spec = IndexSpec(['tenant', 'enabled'])
        doc = {'_id': 'x', 'tenant': 't', 'enabled': False}
        self.assertEqual(len(extract_keys(doc, spec)), 1)

    def test_root_missing_field(self):
        spec = IndexSpec(['tenant', 'enabled'])
        key = extract_keys({'_id': 'x'}, spec)[0]
        self.assertEqual(pairs(key)[0], (TAG_MISSING, None))
        self.assertEqual(pairs(key)[1], (TAG_MISSING, None))

    def test_validate_rejects_nested_arrays_and_bad_id(self):
        with self.assertRaises(ValueError):
            validate_document({'_id': '', 'items': []})
        with self.assertRaises(ValueError):
            validate_document({'_id': 'x', 'items': [{'sku': []}]})
        with self.assertRaises(ValueError):
            validate_document({'_id': 'x', 'items': 'nope'})


if __name__ == '__main__':
    unittest.main()
