"""End-to-end tests for the persistent catalog.

Covers every scenario named in the spec: the canonical mixed/same fixture,
Z/9 replacement, persistence across reopen, atomic put/remove, missing/NULL/
empty/duplicate handling, OR branches, explain accounting, verify, the
selectivity benchmark and randomized differential testing against the
independent oracle.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog as catalog_module  # noqa: E402
from catalog import Catalog  # noqa: E402
from evaluator import query_documents  # noqa: E402
from indexer import TAG_EMPTY, TAG_MISSING, TAG_NULL  # noqa: E402
from tests import gen  # noqa: E402

INDEXES = {
    'main': ['tenant', 'items.sku', 'items.qty'],
    'qty': ['tenant', 'items.qty'],
    'wh': ['tenant', 'items.warehouse'],
    'scalar': ['tenant', 'enabled'],
}

EM_A5 = gen.elem_match_a5()
AND_A5 = gen.plain_and_a5()


class TempCatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='catalog-test-'))
        self.directory = self.tmp / 'store'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, indexes=None, documents=()):
        return Catalog.build(self.directory, indexes or INDEXES, documents)


class CanonicalFixtureTests(TempCatalogTestCase):
    def setUp(self):
        super().setUp()
        self.catalog = self.build(INDEXES, gen.canonical_fixture_documents())

    def test_elem_match_rejects_mixed_but_accepts_same(self):
        self.assertEqual(self.catalog.query(EM_A5), ['same'])

    def test_plain_and_accepts_both(self):
        self.assertEqual(self.catalog.query(AND_A5), ['mixed', 'same'])

    def test_no_nonexistent_cartesian_posting(self):
        postings = self.catalog.dump_index('main')
        mixed = [p for p in postings if p['doc_id'] == 'mixed']
        sku_qty = sorted((p['key'][1][1], p['key'][2][1]) for p in mixed)
        self.assertEqual(sku_qty, [('A', 1), ('B', 5)])

    def test_replace_mixed_with_z9_excludes_it_everywhere(self):
        self.catalog.put({'_id': 'mixed', 'tenant': 't', 'enabled': True,
                          'items': [{'sku': 'Z', 'qty': 9}]})
        self.assertEqual(self.catalog.query(EM_A5), ['same'])
        self.assertEqual(self.catalog.query(AND_A5), ['same'])
        # Old A/1 and B/5 postings for mixed must be gone.
        mixed = [p for p in self.catalog.dump_index('main') if p['doc_id'] == 'mixed']
        self.assertEqual([(p['key'][1][1], p['key'][2][1]) for p in mixed], [('Z', 9)])
        self.assertTrue(self.catalog.verify()['ok'])

    def test_reopen_preserves_everything(self):
        self.catalog.close()
        reopened = Catalog(self.directory)
        self.assertEqual(reopened.query(EM_A5), ['same'])
        self.assertEqual(reopened.query(AND_A5), ['mixed', 'same'])
        self.assertTrue(reopened.verify()['ok'])


class EdgeShapeTests(TempCatalogTestCase):
    def _documents(self):
        return [
            # Repeated identical element collapses to one posting.
            {'_id': 'dup', 'tenant': 't',
             'items': [{'sku': 'A', 'qty': 5}, {'sku': 'A', 'qty': 5}]},
            # Same sku/qty values in a different document.
            {'_id': 'dup2', 'tenant': 't',
             'items': [{'sku': 'A', 'qty': 5}]},
            # Empty array gets an explicit placeholder.
            {'_id': 'empty', 'tenant': 't', 'items': []},
            # Missing array gets a distinct placeholder.
            {'_id': 'missing', 'tenant': 't'},
            # Explicit NULLs alongside missing subfields.
            {'_id': 'nullish', 'tenant': 't', 'items': [
                {'sku': None, 'qty': None, 'warehouse': None},
                {'sku': 'A'},
                {'qty': 7},
            ]},
            # Root field missing and explicit null.
            {'_id': 'rootnull', 'items': [{'sku': 'A', 'qty': 5}]},
        ]

    def test_all_shapes_round_trip_and_match_oracle(self):
        documents = self._documents()
        catalog = self.build(INDEXES, documents)
        queries = [
            {'field': 'items.sku', 'op': 'eq', 'value': 'A'},
            {'field': 'items.sku', 'op': 'eq', 'value': None},
            {'field': 'items.qty', 'op': 'eq', 'value': None},
            {'field': 'items.qty', 'op': 'ge', 'value': 5},
            {'field': 'items.sku', 'op': 'exists', 'value': True},
            {'field': 'tenant', 'op': 'exists', 'value': False},
            {'or': [{'field': 'items.sku', 'op': 'eq', 'value': 'Z'},
                    {'field': 'items.qty', 'op': 'eq', 'value': 7}]},
            {'elemMatch': [{'field': 'items.sku', 'op': 'eq', 'value': 'A'},
                           {'field': 'items.qty', 'op': 'eq', 'value': 5}]},
        ]
        for query in queries:
            self.assertEqual(catalog.query(query), query_documents(query, documents),
                             msg=json.dumps(query))

    def test_empty_and_missing_placeholders_are_distinct_and_explicit(self):
        catalog = self.build(INDEXES, self._documents())
        main = {(p['doc_id'], tuple((t, v) for t, v in p['key']))
                for p in catalog.dump_index('main')}
        empty_keys = [k for doc_id, k in main if doc_id == 'empty']
        missing_keys = [k for doc_id, k in main if doc_id == 'missing']
        self.assertTrue(all(pair[0] == TAG_EMPTY for key in empty_keys for pair in key[1:]))
        self.assertTrue(all(pair[0] == TAG_MISSING for key in missing_keys for pair in key[1:]))
        self.assertNotEqual(empty_keys, missing_keys)

    def test_null_never_merges_with_missing_in_storage(self):
        catalog = self.build(INDEXES, self._documents())
        wh_postings = catalog.dump_index('wh')
        tags = {(p['doc_id'], p['key'][1][0]) for p in wh_postings}
        # nullish document has both an explicit-NULL warehouse element and
        # elements where warehouse is missing; both tags must appear.
        self.assertIn(('nullish', TAG_NULL), tags)
        self.assertIn(('nullish', TAG_MISSING), tags)

    def test_duplicate_element_is_one_posting_but_one_document(self):
        catalog = self.build(INDEXES, self._documents())
        rows = [p for p in catalog.dump_index('main') if p['doc_id'] == 'dup']
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            catalog.query({'field': 'items.sku', 'op': 'eq', 'value': 'A'}),
            ['dup', 'dup2', 'nullish', 'rootnull'],
        )


class PersistenceAndMutationTests(TempCatalogTestCase):
    def test_remove_leaves_no_candidates_and_reopen_stays_clean(self):
        documents = gen.canonical_fixture_documents()
        catalog = self.build(INDEXES, documents)
        self.assertTrue(catalog.remove('mixed'))
        self.assertFalse(catalog.remove('mixed'))  # second remove is a no-op
        for name in catalog.index_specs:
            self.assertFalse(any(
                p['doc_id'] == 'mixed' for p in catalog.dump_index(name[0])))
        self.assertEqual(catalog.query(AND_A5), ['same'])
        catalog.close()

        reopened = Catalog(self.directory)
        self.assertEqual(reopened.query(AND_A5), ['same'])
        self.assertTrue(reopened.verify()['ok'])

    def test_put_old_keys_exit_new_keys_enter_in_one_commit(self):
        catalog = self.build(INDEXES, gen.canonical_fixture_documents())
        catalog.put({'_id': 'mixed', 'tenant': 'u', 'enabled': False,
                     'items': [{'sku': 'C', 'qty': 2, 'warehouse': 'w1'}]})
        # tenant changed: old 't' postings must be gone.
        for posting in catalog.dump_index('main'):
            if posting['doc_id'] == 'mixed':
                self.assertEqual(posting['key'][0], ('T', 'u'))
        self.assertEqual(
            catalog.query({'field': 'tenant', 'op': 'eq', 'value': 't'}),
            ['same'],
        )
        self.assertTrue(catalog.verify()['ok'])

    def test_failed_write_rolls_back_document_and_indexes(self):
        catalog = self.build(INDEXES, gen.canonical_fixture_documents())
        original_extract = catalog_module.extract_keys

        def exploding_extract(document, spec):
            if document['_id'] == 'mixed':
                raise RuntimeError('simulated index failure')
            return original_extract(document, spec)

        catalog_module.extract_keys = exploding_extract
        try:
            with self.assertRaises(RuntimeError):
                catalog.put({'_id': 'mixed', 'tenant': 't', 'enabled': True,
                             'items': [{'sku': 'Z', 'qty': 9}]})
        finally:
            catalog_module.extract_keys = original_extract

        # The old document and its postings must be fully intact.
        payloads = {d['_id']: d for d in catalog.documents()}
        self.assertEqual([i['sku'] for i in payloads['mixed']['items']], ['A', 'B'])
        self.assertEqual(catalog.query(AND_A5), ['mixed', 'same'])
        self.assertTrue(catalog.verify()['ok'])

    def test_verify_detects_manual_corruption(self):
        catalog = self.build(INDEXES, gen.canonical_fixture_documents())
        catalog.connection.execute("DELETE FROM idx_main WHERE doc_id='same'")
        report = catalog.verify()
        self.assertFalse(report['ok'])
        self.assertTrue(any('same' in problem for problem in report['problems']))

    def test_cross_array_index_rejected_before_storage_touched(self):
        with self.assertRaises(ValueError):
            Catalog.build(self.directory, {'bad': ['items.sku', 'other.kind']}, [])
        self.assertFalse(self.directory.exists())


class ExplainTests(TempCatalogTestCase):
    def test_explain_reports_bounds_binding_and_residual(self):
        catalog = self.build(INDEXES, gen.canonical_fixture_documents())
        explanation = catalog.explain(EM_A5)
        self.assertEqual(explanation['strategy'], 'index')
        self.assertEqual(explanation['matches'], ['same'])
        self.assertTrue(explanation['key_bounds'])
        bound = explanation['key_bounds'][0]
        self.assertTrue(bound['correlated'])
        self.assertEqual(
            explanation['element_binding']['correlated_scans'][0]['basis'],
            'elemMatch')
        # The full AST remains the residual condition.
        self.assertEqual(explanation['residual_conditions'], EM_A5)
        self.assertGreaterEqual(explanation['keys_accessed'], 1)
        self.assertEqual(explanation['candidates'], explanation['fetches'])
        self.assertEqual(explanation['residual_checks'], explanation['candidates'])

    def test_full_scan_is_justified_and_logged(self):
        catalog = self.build({'scalar': ['tenant', 'enabled']},
                             gen.canonical_fixture_documents())
        explanation = catalog.explain(EM_A5)
        self.assertEqual(explanation['strategy'], 'full_scan')
        self.assertTrue(explanation['full_scan_reason'])
        # Oracle still gets the right answer through the residual pass.
        self.assertEqual(explanation['matches'], ['same'])
        log = catalog.query_log()
        self.assertEqual(log[0]['strategy'], 'full_scan')

    def test_counters_come_from_real_execution(self):
        catalog = self.build(INDEXES, gen.canonical_fixture_documents())
        explanation = catalog.explain(AND_A5)
        for trace in explanation['scan_traces']:
            if trace['op'] == 'scan':
                self.assertGreaterEqual(trace['keys_accessed'], 0)
                self.assertGreaterEqual(trace['candidate_docs'], 0)
        self.assertGreaterEqual(explanation['fetches'], 1)
        self.assertGreaterEqual(explanation['residual_checks'], 1)

    def test_index_candidate_is_a_superset_not_an_answer(self):
        # A narrow index bound would over-select: sku=A alone matches mixed
        # too, and the residual elemMatch must be the thing that removes it.
        catalog = self.build({'main': ['tenant', 'items.sku', 'items.qty']},
                             gen.canonical_fixture_documents())
        # Partial index: only sku is servable, qty bound is residual.
        explanation = catalog.explain({
            'and': [{'field': 'items.sku', 'op': 'eq', 'value': 'A'},
                    {'field': 'items.qty', 'op': 'eq', 'value': 5}]})
        # sku=A alone is a superset containing both documents; the residual
        # pass is what makes the final answer correct.
        self.assertGreaterEqual(explanation['candidates'], 2)
        # Final answer still equals the oracle; residual AST is the full query.
        self.assertEqual(explanation['matches'], ['mixed', 'same'])
        self.assertEqual(explanation['residual_conditions'], {
            'and': [{'field': 'items.sku', 'op': 'eq', 'value': 'A'},
                    {'field': 'items.qty', 'op': 'eq', 'value': 5}]})
        self.assertGreaterEqual(explanation['residual_checks'], explanation['candidates'])


class SelectivityBenchmarkTests(TempCatalogTestCase):
    def test_tenant_sku_equality_fetches_at_most_30(self):
        documents = gen.benchmark_documents()
        catalog = self.build({'main': ['tenant', 'items.sku', 'items.qty']},
                             documents)
        query = {'and': [
            {'field': 'tenant', 'op': 'eq', 'value': 't'},
            {'field': 'items.sku', 'op': 'eq', 'value': 'wanted'},
        ]}
        explanation = catalog.explain(query)
        self.assertEqual(len(explanation['matches']), 10)
        self.assertLessEqual(explanation['fetches'], 30)
        self.assertEqual(explanation['strategy'], 'index')
        # The access log is retained locally and agrees with the explain.
        logged = catalog.index_access_log()[0]
        self.assertEqual(logged['fetches'], explanation['fetches'])
        self.assertEqual(logged['matches'], 10)
        # Sanity vs oracle.
        self.assertEqual(explanation['matches'], query_documents(query, documents))


class DifferentialFuzzTests(TempCatalogTestCase):
    def test_randomized_queries_match_oracle_and_survive_reopen(self):
        documents = gen.make_documents(seed=423, count=120)
        catalog = self.build(INDEXES, documents)
        queries = gen.make_queries(seed=9423, count=300)
        for query in queries:
            expected = query_documents(query, documents)
            self.assertEqual(catalog.query(query), expected,
                             msg=json.dumps(query, ensure_ascii=False))
        self.assertTrue(catalog.verify()['ok'])

        # Mutate: replace some, remove others, add new edge-case documents.
        rng_docs = gen.make_documents(seed=77, count=40)
        for document in rng_docs[:20]:
            catalog.put(document)
        for document in documents[::17]:
            catalog.remove(document['_id'])
        catalog.put({'_id': 'fresh-empty', 'tenant': 't', 'items': []})
        catalog.put({'_id': 'fresh-null', 'tenant': None,
                     'items': [{'warehouse': None}]})
        live = catalog.documents()
        catalog.close()

        reopened = Catalog(self.directory)
        self.assertTrue(reopened.verify()['ok'])
        for query in gen.make_queries(seed=2024, count=200):
            self.assertEqual(reopened.query(query), query_documents(query, live),
                             msg=json.dumps(query, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
