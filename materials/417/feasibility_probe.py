"""Public benchmark feasibility: selective pruning with real access budgets.

Runs the fixed alpha/top-1/block_size=5 case on the repaired engine and
asserts the expected winner, that nonempty blocks are actually skipped, and
that both scored count and query-phase raw-document accesses stay below 50.
The raw-access probe separately demonstrates why the old in-query statistics
scan fails that budget.
"""
import json
from pathlib import Path

from inverted import Index
from oracle import oracle
from ranker import query


def run():
    case = json.loads(Path(__file__).with_name(
        'benchmark.json').read_text())
    index = Index(case['docs'], case['block_size'])
    rows, audit = query(index, case['terms'], case['k'])
    oracle_rows, _ = oracle(index, case['terms'], case['k'])
    assert rows[0][0] == case['expected_id']
    assert rows == oracle_rows
    assert audit['scored'] < case['max_scored_exclusive']
    assert audit['raw_document_accesses'] < 50
    assert audit['skipped_nonempty_blocks'] >= 1
    return {
        'result': rows,
        'oracle_result': oracle_rows,
        'audit': {
            'skipped': audit['skipped'],
            'skipped_nonempty_blocks': audit['skipped_nonempty_blocks'],
            'scored': audit['scored'],
            'raw_document_accesses': audit['raw_document_accesses'],
            'metadata_accesses': audit['metadata_accesses'],
        },
        'statistics_used': audit['statistics_used'],
        'pruning_observed': True,
        'scope': 'Repaired engine on the public selective case; raw payload '
                 'reads and scored docs both under the exclusive budget.',
    }


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
