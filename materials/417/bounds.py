"""Numeric block bounds for one fixed (corpus revision, k1, b) configuration.

The live index stores parameter-independent envelopes (see envelopes.py) and
the ranker exports safe numeric bounds at query time. This module additionally
provides a snapshot-style numeric bound table, useful for inspection, tests and
caching: a table is only valid for the exact basis it was built under, and
callers must re-check that basis before relying on any cached value.

``build_bound`` is retained in its original signature so legacy callers that
hand it (doc_ids, docs, stats) keep working; it now records the basis too.
"""
from envelopes import block_upper_bound, term_upper_bound
from scorer import DEFAULT_B, DEFAULT_K1


def build_bound(doc_ids, docs, stats, k1=DEFAULT_K1, b=DEFAULT_B):
    """Exact numeric per-term max over the given raw documents.

    This reads the supplied payloads directly and is valid only for the given
    statistics revision and parameters; it is a snapshot, not a live bound.
    """
    from scorer import contribution_p
    terms = {term for rid in doc_ids for term in docs[rid]}
    return {
        term: max(contribution_p(term, docs[rid], stats, k1, b)
                  for rid in doc_ids)
        for term in terms
    }


def export_block_bounds(index, terms, k1=DEFAULT_K1, b=DEFAULT_B):
    """Export current qualified numeric bounds for every block.

    Returns (bounds_per_block, basis) where basis must match the query's
    revision/parameters before any bound may be used for pruning.
    """
    stats = index.current_stats()
    bounds = []
    for envelope in index.envelopes:
        bounds.append({
            term: term_upper_bound(term, envelope, stats, k1, b)
            for term in terms
            if term in envelope
        })
    basis = {
        'revision': stats['revision'],
        'n': stats['n'],
        'avgdl': stats['avgdl'],
        'df': {t: stats['df'].get(t, 0) for t in terms},
        'k1': k1, 'b': b,
    }
    return bounds, basis


def qualified_block_bound(index, block_no, terms, k1, b,
                          expected_revision=None):
    """Return (upper_bound, basis) after an explicit applicability check."""
    stats = index.current_stats()
    if expected_revision is not None and \
            stats['revision'] != expected_revision:
        raise StaleBoundError(
            f'cached bound revision {expected_revision} != current '
            f'{stats["revision"]}; rebuild required')
    envelope = index.envelopes[block_no]
    upper = block_upper_bound(terms, envelope, stats, k1, b)
    return upper, {
        'revision': stats['revision'], 'n': stats['n'],
        'avgdl': stats['avgdl'], 'k1': k1, 'b': b,
        'df': {t: stats['df'].get(t, 0) for t in terms},
    }


class StaleBoundError(RuntimeError):
    pass
