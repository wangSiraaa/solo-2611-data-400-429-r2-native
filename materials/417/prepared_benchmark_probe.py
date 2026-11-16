"""Feasibility of write-prepared metadata for the one public selective case.

Only the known first block is scored. All other blocks' bounds are inspected
to prove they are below that winner. This is not a general query engine,
incremental statistics maintainer, persistent index or update implementation.
"""
import json
from pathlib import Path

from bounds import build_bound
from inverted import Index
from raw_access_probe import CountedDocuments
from scorer import score, statistics


def snapshot_check(case, changed=False):
    index = Index(case['docs'], case['block_size'])
    if changed:
        # A real legal replacement changes df/length inputs before preparation.
        index.put('D001', ['alpha'] * 3 + ['padding'] * 197)
        index.remove('D099')
    docs = CountedDocuments(index.docs)
    docs.phase = 'update_prepare' if changed else 'build_prepare'
    current = statistics(docs)
    blocks = [[identity for identity in block if identity in docs]
              for block in index.blocks]
    prepared = [build_bound(block, docs, current) if block else {}
                for block in blocks]
    prepare_phase = docs.phase
    preparation_accesses = docs.document_accesses[prepare_phase]
    preparation_tokens = docs.token_accesses[prepare_phase]

    # This is a feasibility check for this fixed selective block distribution.
    # It does not select an arbitrary next block or solve arbitrary queries.
    docs.phase = 'query'
    first_block = [(identity, score(case['terms'], docs[identity], current))
                   for identity in blocks[0]]
    winner = min(first_block, key=lambda pair: (-pair[1], pair[0].encode()))
    skipped_upper = [sum(bound.get(term, 0.0) for term in case['terms'])
                     for bound in prepared[1:]]
    assert all(upper < winner[1] for upper in skipped_upper)
    query_accesses = docs.document_accesses['query']
    query_tokens = docs.token_accesses['query']

    docs.phase = 'independent_full_score_oracle'
    oracle = min(((identity, score(case['terms'], tokens, current))
                  for identity, tokens in docs.items()),
                 key=lambda pair: (-pair[1], pair[0].encode()))
    assert winner == oracle
    assert query_accesses < 50
    return {
        'phase': prepare_phase,
        'current_metadata': {'N': current['n'], 'df': dict(current['df']),
                             'avgdl': current['avgdl'],
                             'total_length': sum(map(len, case['docs'].values())) - (200 if changed else 0)},
        'preparation_raw_document_accesses': preparation_accesses,
        'preparation_token_accesses': preparation_tokens,
        'query_raw_document_accesses': query_accesses,
        'query_token_accesses': query_tokens,
        'prepared_bound_blocks_inspected': len(prepared),
        'nonempty_blocks_safely_skipped': sum(bool(block) for block in blocks[1:]),
        'winner': winner,
        'oracle_winner': oracle,
        'query_limit_exclusive': 50,
    }


def run():
    case = json.loads(Path(__file__).with_name('benchmark.json').read_text())
    return {
        'scope': 'Fixed alpha/top1/block_size5 case only, before and after one actual replacement+removal; no general target implementation.',
        'checks': [snapshot_check(case), snapshot_check(case, changed=True)],
        'accounting': 'Build/update preparation and full-score oracle are separately counted. No query statistics or bounds rebuild is excluded from query raw-access accounting.',
    }


if __name__ == '__main__':
    print(json.dumps(run()))
