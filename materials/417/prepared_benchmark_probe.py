"""Write-prepared metadata keeps queries cheap even after a real update.

Preparation (envelope rebuild, statistic maintenance) happens during
build/put/remove and is metered in that phase from tf/length metadata, not in
the query. The query then exports numeric bounds under the live revision and
reads payloads only for scored documents. This runs the general engine (not a
fixed-block shortcut) before and after an actual replacement + removal, and
cross-checks every result with the full-scoring oracle.
"""
import json
from pathlib import Path

from inverted import Index
from oracle import oracle
from ranker import query


def snapshot_check(case, changed=False):
    index = Index(case['docs'], case['block_size'])
    index.store.reset_counters(phase='build')
    if changed:
        index.store.phase = 'update_prepare'
        index.put('D001', ['alpha'] * 3 + ['padding'] * 197)
        index.remove('D099')
        update_reads = index.store.document_reads['update_prepare']
    else:
        update_reads = 0

    # Query window: only reads performed from here count against the budget.
    index.store.phase = 'query'
    rows, audit = query(index, case['terms'], case['k'])
    oracle_rows, oracle_info = oracle(index, case['terms'], case['k'])
    assert rows == oracle_rows
    assert audit['raw_document_accesses'] < 50
    assert audit['skipped_nonempty_blocks'] >= 1

    current = audit['statistics_used']
    return {
        'changed': changed,
        'current_metadata': {
            'revision': audit['revision'],
            'N': current['n'],
            'df': current['df'],
            'avgdl': current['avgdl'],
            'total_length': current['total_length'],
        },
        'update_phase_raw_document_accesses': update_reads,
        'query_raw_document_accesses': audit['raw_document_accesses'],
        'query_token_accesses': audit['raw_token_accesses'],
        'query_metadata_accesses': audit['metadata_accesses'],
        'nonempty_blocks_safely_skipped': audit['skipped_nonempty_blocks'],
        'winner': rows,
        'oracle_winner': oracle_rows,
        'query_limit_exclusive': 50,
        'accounting': 'preparation and query windows are separate; the query '
                      'performs no statistics or envelope rebuild scan.',
    }


def run():
    case = json.loads(Path(__file__).with_name(
        'benchmark.json').read_text())
    return {
        'scope': 'General repaired engine on the public case, before and after '
                 'one real replacement+removal; oracle verified both times.',
        'checks': [snapshot_check(case), snapshot_check(case, changed=True)],
    }


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
