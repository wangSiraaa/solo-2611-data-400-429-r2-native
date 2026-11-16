"""Planner tests: correlation rules, prefix gaps, full-scan fallbacks."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indexer import IndexSpec  # noqa: E402
from planner import collect_scans, leaf_bound, plan_query  # noqa: E402


MAIN = ('main', IndexSpec(['tenant', 'items.sku', 'items.qty']))
QTY = ('qty', IndexSpec(['tenant', 'items.qty']))
SCALAR = ('scalar', IndexSpec(['tenant', 'enabled']))
SPECS = [MAIN, QTY, SCALAR]


def leaf(field, op, value):
    return {'field': field, 'op': op, 'value': value}


class BoundTranslationTests(unittest.TestCase):
    def test_null_and_missing_are_distinct_bounds(self):
        null_bound = leaf_bound(leaf('tenant', 'eq', None))
        self.assertEqual(null_bound, {'op': 'eq', 'tag': 'N', 'value': None})

    def test_bool_vs_integer_bounds(self):
        self.assertEqual(leaf_bound(leaf('enabled', 'eq', True))['tag'], 'B')
        self.assertEqual(leaf_bound(leaf('items.qty', 'eq', 1))['tag'], 'I')

    def test_exists_false_array_leaf_not_servable(self):
        self.assertIsNone(leaf_bound(leaf('items.sku', 'exists', False)))
        root_bound = leaf_bound(leaf('tenant', 'exists', False))
        self.assertEqual(root_bound, {'op': 'eq', 'tag': 'M', 'value': None})

    def test_range_bound_is_integer_typed(self):
        bound = leaf_bound(leaf('items.qty', 'ge', 5))
        self.assertEqual(bound, {'op': 'ge', 'tag': 'I', 'value': 5})


class CorrelationTests(unittest.TestCase):
    def test_elem_match_gets_one_correlated_scan(self):
        ast = {'elemMatch': [
            leaf('items.sku', 'eq', 'A'), leaf('items.qty', 'eq', 5)]}
        plan = plan_query(ast, SPECS)
        scans = collect_scans(plan['tree'])
        self.assertEqual(len(scans), 1)
        self.assertTrue(scans[0]['correlated'])
        self.assertEqual(scans[0]['binding'], 'elemMatch')
        fields = {b['field'] for b in scans[0]['bounds']}
        self.assertEqual(fields, {'items.sku', 'items.qty'})

    def test_plain_and_never_correlates_in_one_scan(self):
        ast = {'and': [leaf('items.sku', 'eq', 'A'),
                       leaf('items.qty', 'eq', 5)]}
        plan = plan_query(ast, SPECS)
        scans = collect_scans(plan['tree'])
        self.assertEqual(plan['tree']['op'], 'intersect')
        self.assertTrue(all(not scan['correlated'] for scan in scans))
        # Two distinct indexes serve the two leaves separately.
        self.assertEqual(len({scan['index_name'] for scan in scans}), 2)

    def test_array_slot_gap_rejects_single_scan(self):
        # Only the main index exists; qty sits behind an unbounded sku slot,
        # so a qty leaf cannot scan main alone - it falls back to full scan.
        ast = leaf('items.qty', 'eq', 5)
        plan = plan_query(ast, [MAIN])
        self.assertEqual(plan['strategy'], 'full_scan')
        self.assertIn('no index', plan['full_scan_reason'])

    def test_leading_root_gap_is_expanded_not_rejected(self):
        ast = {'elemMatch': [
            leaf('items.sku', 'eq', 'A'), leaf('items.qty', 'eq', 5)]}
        plan = plan_query(ast, [MAIN])
        scan = collect_scans(plan['tree'])[0]
        self.assertEqual(scan['expanded_root_gaps'], [0])

    def test_or_with_unservable_branch_full_scans(self):
        ast = {'or': [leaf('items.sku', 'eq', 'A'),
                      leaf('items.warehouse', 'eq', 'w1')]}
        plan = plan_query(ast, SPECS)
        self.assertEqual(plan['strategy'], 'full_scan')
        self.assertIn('OR branch', plan['full_scan_reason'])

    def test_and_with_partial_coverage_keeps_residual(self):
        ast = {'and': [leaf('items.sku', 'eq', 'A'),
                       leaf('items.warehouse', 'eq', 'w1')]}
        plan = plan_query(ast, SPECS)
        self.assertEqual(plan['strategy'], 'index')
        # Only the sku leaf has an index scan; warehouse stays residual.
        self.assertEqual(len(collect_scans(plan['tree'])), 1)
        self.assertEqual(plan['residual_ast'], ast)

    def test_union_of_or_branches(self):
        ast = {'or': [leaf('items.sku', 'eq', 'A'),
                      leaf('items.qty', 'eq', 5)]}
        plan = plan_query(ast, SPECS)
        self.assertEqual(plan['tree']['op'], 'union')
        self.assertEqual(len(collect_scans(plan['tree'])), 2)

    def test_candidate_indexes_listed(self):
        ast = leaf('enabled', 'eq', True)
        plan = plan_query(ast, SPECS)
        self.assertEqual([c['name'] for c in plan['candidate_indexes']], ['scalar'])


if __name__ == '__main__':
    unittest.main()
