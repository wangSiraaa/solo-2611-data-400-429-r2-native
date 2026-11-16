"""Selectivity benchmark over the real persistent multikey catalog.

500 same-tenant documents, only 10 carrying the target sku.  The tenant/sku
equality query must reach no more than 30 documents on the table fetch; the
full index access log is retained and returned.  This proves selective
access for the targeted shape - it does not promise every query beats a
full scan.
"""

import json
import shutil
import tempfile
from pathlib import Path

from catalog import Catalog
from evaluator import query_documents
from tests.gen import benchmark_documents

FETCH_LIMIT = 30


def run():
    documents = benchmark_documents()
    workdir = Path(tempfile.mkdtemp(prefix='q423-benchmark-'))
    try:
        catalog = Catalog.build(
            workdir / 'store',
            {'main': ['tenant', 'items.sku', 'items.qty']},
            documents,
        )
        query = {
            'and': [
                {'field': 'tenant', 'op': 'eq', 'value': 't'},
                {'field': 'items.sku', 'op': 'eq', 'value': 'wanted'},
            ]
        }
        explanation = catalog.explain(query)
        log_row = catalog.index_access_log()[0]
        assert explanation['matches'] == query_documents(query, documents)
        assert explanation['fetches'] <= FETCH_LIMIT, explanation['fetches']
        result = {
            'scope': 'real compound multikey catalog; selective tenant/sku access',
            'source_documents': len(documents),
            'expected_matches': 10,
            'actual_matches': explanation['matches'],
            'strategy': explanation['strategy'],
            'index_ranges': explanation['ranges'],
            'keys_accessed': explanation['keys_accessed'],
            'candidate_documents': explanation['candidates'],
            'documents_fetched': explanation['fetches'],
            'residual_checks': explanation['residual_checks'],
            'required_fetch_limit': FETCH_LIMIT,
            'within_fetch_limit': explanation['fetches'] <= FETCH_LIMIT,
            'retained_access_log': log_row,
            'verify': catalog.verify(),
        }
        catalog.close()
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
