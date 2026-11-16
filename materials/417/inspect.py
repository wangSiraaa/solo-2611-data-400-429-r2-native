"""Observable index state: current statistics, bound applicability and cost.

Everything here is answered from maintained metadata and counters. No query
has to run first to learn whether the statistics or block summaries are
current: each carries the revision it was derived under, and the live
revision is reported alongside.
"""
from bounds import export_block_bounds
from scorer import DEFAULT_B, DEFAULT_K1


def inspect(index, terms=None, k1=DEFAULT_K1, b=DEFAULT_B):
    stats = index.current_stats()
    block_summaries = []
    for number, (block, envelope) in enumerate(
            zip(index.blocks, index.envelopes)):
        block_summaries.append({
            'block': number,
            'active_doc_ids': list(block),
            'active_count': len(block),
            'envelope_terms': sorted(envelope),
            # term -> [tf_max, len_min]; parameter-independent facts
            'envelope': {term: list(entry) for term, entry in envelope.items()},
            'envelope_revision': stats['revision'],
            'current': True,
        })
    report = {
        'revision': stats['revision'],
        'block_size': index.block_size,
        'active_document_count_n': stats['n'],
        'total_length': stats['total_length'],
        'avgdl': stats['avgdl'],
        'df': dict(stats['df']),
        'vocabulary_size': len(index.df),
        'block_count': len(index.blocks),
        'nonempty_block_count': sum(1 for block in index.blocks if block),
        'updates_since_build': index.update_count,
        'statistics_applicability': {
            'derived_under_revision': stats['revision'],
            'live_revision': index.revision,
            'current': stats['revision'] == index.revision,
            'source': 'incrementally maintained per-doc tf/length metadata; '
                      'no payload scan performed at query time',
        },
        'envelope_applicability': {
            'kind': 'parameter-independent (tf_max, len_min) per block/term',
            'rebuilt_on': 'every build/put/remove touching the block',
            'live_revision': index.revision,
            'note': 'numeric bounds are exported per query under the live '
                    'revision and that query k1/b; stale numbers are never used',
        },
        'blocks': block_summaries,
        'update_history': list(index.history),
        'query_log': list(index.query_log),
        'payload_store_accounting': index.store.read_account(),
    }
    if terms is not None:
        numeric, basis = export_block_bounds(index, list(terms), k1, b)
        report['exported_numeric_bounds'] = {
            'terms': list(terms), 'parameters': {'k1': k1, 'b': b},
            'basis': basis,
            'per_block': numeric,
            'qualification': 'valid only while live revision == basis.revision '
                             'and query parameters == basis.k1/b',
        }
    return report


def skip_evidence(audit):
    """Extract just the per-skip proof lines from a query audit."""
    return [
        {
            'block': entry['block'],
            'block_upper_bound': entry['block_upper_bound'],
            'threshold': entry['threshold'],
            'term_bounds': entry['term_bounds'],
            'basis': {
                'revision': entry['basis_revision'],
                'n': entry['basis_n'],
                'avgdl': entry['basis_avgdl'],
                'df': entry['basis_df'],
                'k1': entry['basis_k1'],
                'b': entry['basis_b'],
            },
            'proof': entry['proof'],
        }
        for entry in audit['block_decisions']
        if entry['decision'] == 'skipped'
    ]
