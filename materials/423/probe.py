"""End-to-end probe for the persistent compound multikey catalog.

Run from this directory:

    python3 probe.py

It rebuilds a catalog from fixture.json in a local directory and demonstrates
the three defects called out in the original README, now fixed:

1. no cartesian same-element combination (mixed is rejected by elemMatch);
2. plain AND still accepts mixed because different elements may satisfy it;
3. put truly replaces old index postings, including after reopen.
"""

import json
import shutil
import tempfile
from pathlib import Path

from catalog import Catalog
from evaluator import query_documents

ROOT = Path(__file__).resolve().parent


def run():
    fixture = json.loads((ROOT / 'fixture.json').read_text(encoding='utf-8'))
    workdir = Path(tempfile.mkdtemp(prefix='q423-probe-'))
    try:
        catalog = Catalog.build(
            workdir / 'store',
            {'main': fixture['index']},
            fixture['documents'],
        )
        elem_query = fixture['queries'][0]
        and_query = {
            'and': [
                {'field': 'items.sku', 'op': 'eq', 'value': 'A'},
                {'field': 'items.qty', 'op': 'eq', 'value': 5},
            ]
        }

        elem_before = catalog.query(elem_query)
        and_before = catalog.query(and_query)
        mixed_keys_before = sorted(
            [entry['key'] for entry in catalog.dump_index('main')
             if entry['doc_id'] == 'mixed']
        )
        catalog.close()

        # Reopen: persistence must preserve documents and index together.
        catalog = Catalog(workdir / 'store')
        catalog.put({
            '_id': 'mixed', 'tenant': 't', 'enabled': True,
            'items': [{'sku': 'Z', 'qty': 9}],
        })
        elem_after = catalog.query(elem_query)
        and_after = catalog.query(and_query)
        mixed_keys_after = sorted(
            [entry['key'] for entry in catalog.dump_index('main')
             if entry['doc_id'] == 'mixed']
        )
        report = {
            'elemMatch_rejects_mixed_accepts_same': elem_before,
            'plain_and_accepts_both_documents': and_before,
            'oracle_plain_and': query_documents(and_query, fixture['documents']),
            'mixed_postings_before_put': mixed_keys_before,
            'cartesian_A5_absent': not any(
                key[1] == ('T', 'A') and key[2] == ('I', 5)
                for key in mixed_keys_before
            ),
            'after_Z9_elemMatch': elem_after,
            'after_Z9_plain_and': and_after,
            'mixed_postings_after_put': mixed_keys_after,
            'verify': catalog.verify(),
        }
        catalog.close()
        return report
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
