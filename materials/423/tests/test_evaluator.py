"""Tests for the independent document/element interpreter (the oracle)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluator import MISSING, evaluate, leaf_on_value, query_documents  # noqa: E402


class LeafSemanticsTests(unittest.TestCase):
    def test_missing_never_matches_comparisons(self):
        for op in ('eq', 'ge', 'le'):
            self.assertFalse(leaf_on_value({'op': op, 'value': 1}, MISSING))

    def test_exists_distinguishes_missing_and_null(self):
        self.assertTrue(leaf_on_value({'op': 'exists', 'value': True}, MISSING) is False)
        self.assertFalse(leaf_on_value({'op': 'exists', 'value': True}, MISSING))
        self.assertTrue(leaf_on_value({'op': 'exists', 'value': True}, None))
        self.assertTrue(leaf_on_value({'op': 'exists', 'value': False}, MISSING))
        self.assertFalse(leaf_on_value({'op': 'exists', 'value': False}, None))

    def test_eq_null_only_matches_explicit_null(self):
        self.assertTrue(leaf_on_value({'op': 'eq', 'value': None}, None))
        self.assertFalse(leaf_on_value({'op': 'eq', 'value': None}, MISSING))
        self.assertFalse(leaf_on_value({'op': 'eq', 'value': None}, 0))
        self.assertFalse(leaf_on_value({'op': 'eq', 'value': None}, ''))

    def test_bool_not_equal_to_integer(self):
        self.assertTrue(leaf_on_value({'op': 'eq', 'value': True}, True))
        self.assertFalse(leaf_on_value({'op': 'eq', 'value': 1}, True))
        self.assertFalse(leaf_on_value({'op': 'eq', 'value': True}, 1))

    def test_ranges_integers_only(self):
        self.assertTrue(leaf_on_value({'op': 'ge', 'value': 5}, 5))
        self.assertFalse(leaf_on_value({'op': 'ge', 'value': 5}, True))
        self.assertFalse(leaf_on_value({'op': 'ge', 'value': 5}, None))
        self.assertFalse(leaf_on_value({'op': 'ge', 'value': 5}, MISSING))
        self.assertTrue(leaf_on_value({'op': 'le', 'value': 5}, 4))
        self.assertFalse(leaf_on_value({'op': 'le', 'value': 5}, '5'))

    def test_text_equality(self):
        self.assertTrue(leaf_on_value({'op': 'eq', 'value': 'A'}, 'A'))
        self.assertFalse(leaf_on_value({'op': 'eq', 'value': 'A'}, 'a'))


class DocumentSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.mixed = {'_id': 'mixed', 'tenant': 't',
                      'items': [{'sku': 'A', 'qty': 1}, {'sku': 'B', 'qty': 5}]}
        self.same = {'_id': 'same', 'tenant': 't',
                     'items': [{'sku': 'A', 'qty': 5}]}
        self.docs = [self.mixed, self.same]

    def test_elem_match_binds_one_element(self):
        query = {'elemMatch': [
            {'field': 'items.sku', 'op': 'eq', 'value': 'A'},
            {'field': 'items.qty', 'op': 'eq', 'value': 5},
        ]}
        self.assertEqual(query_documents(query, self.docs), ['same'])

    def test_plain_and_allows_different_elements(self):
        query = {'and': [
            {'field': 'items.sku', 'op': 'eq', 'value': 'A'},
            {'field': 'items.qty', 'op': 'eq', 'value': 5},
        ]}
        self.assertEqual(query_documents(query, self.docs), ['mixed', 'same'])

    def test_after_z9_both_exclude(self):
        z9 = {'_id': 'mixed', 'tenant': 't',
              'items': [{'sku': 'Z', 'qty': 9}]}
        em = {'elemMatch': [
            {'field': 'items.sku', 'op': 'eq', 'value': 'A'},
            {'field': 'items.qty', 'op': 'eq', 'value': 5},
        ]}
        and_q = {'and': [
            {'field': 'items.sku', 'op': 'eq', 'value': 'A'},
            {'field': 'items.qty', 'op': 'eq', 'value': 5},
        ]}
        self.assertEqual(query_documents(em, [z9, self.same]), ['same'])
        self.assertEqual(query_documents(and_q, [z9, self.same]), ['same'])

    def test_or_branch(self):
        query = {'or': [
            {'field': 'items.sku', 'op': 'eq', 'value': 'B'},
            {'field': 'items.qty', 'op': 'eq', 'value': 5},
        ]}
        self.assertEqual(query_documents(query, self.docs), ['mixed', 'same'])

    def test_empty_and_missing_arrays(self):
        docs = [
            {'_id': 'empty', 'items': []},
            {'_id': 'absent', 'tenant': 't'},
        ]
        present = {'field': 'items.sku', 'op': 'exists', 'value': True}
        absent = {'field': 'items.sku', 'op': 'exists', 'value': False}
        self.assertEqual(query_documents(present, docs), [])
        self.assertEqual(query_documents(absent, docs), ['absent', 'empty'])

    def test_elem_match_on_empty_array_false(self):
        em = {'elemMatch': [{'field': 'items.sku', 'op': 'eq', 'value': 'A'}]}
        self.assertFalse(evaluate(em, {'_id': 'e', 'items': []}))
        self.assertFalse(evaluate(em, {'_id': 'm'}))

    def test_root_fields_are_document_scoped(self):
        doc = {'_id': 'r', 'enabled': True, 'tenant': 't'}
        self.assertTrue(evaluate({'field': 'enabled', 'op': 'eq', 'value': True}, doc))
        self.assertTrue(evaluate({'field': 'tenant', 'op': 'exists', 'value': True}, doc))
        self.assertFalse(evaluate({'field': 'enabled', 'op': 'exists', 'value': False}, doc))

    def test_nested_and_or_composition(self):
        query = {'and': [
            {'field': 'tenant', 'op': 'eq', 'value': 't'},
            {'or': [
                {'field': 'items.sku', 'op': 'eq', 'value': 'Z'},
                {'field': 'items.qty', 'op': 'ge', 'value': 5},
            ]},
        ]}
        self.assertEqual(query_documents(query, self.docs), ['mixed', 'same'])

    def test_results_codepoint_sorted_unique(self):
        docs = [
            {'_id': 'B', 'items': [{'sku': 'A'}]},
            {'_id': 'a', 'items': [{'sku': 'A'}, {'sku': 'A'}]},
            {'_id': 'A', 'items': [{'sku': 'A'}]},
        ]
        query = {'field': 'items.sku', 'op': 'eq', 'value': 'A'}
        # Uppercase letters precede lowercase in codepoint order.
        self.assertEqual(query_documents(query, docs), ['A', 'B', 'a'])

    def test_partial_element_fields_and_null(self):
        doc = {'_id': 'p', 'items': [
            {'sku': 'A'}, {'qty': 5}, {'sku': None, 'qty': None},
        ]}
        self.assertTrue(evaluate(
            {'field': 'items.sku', 'op': 'eq', 'value': None}, doc))
        self.assertTrue(evaluate(
            {'field': 'items.qty', 'op': 'eq', 'value': None}, doc))
        self.assertTrue(evaluate(
            {'field': 'items.sku', 'op': 'eq', 'value': 'A'}, doc))


if __name__ == '__main__':
    unittest.main()
