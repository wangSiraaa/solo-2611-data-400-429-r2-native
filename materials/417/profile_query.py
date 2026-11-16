"""Configurable caller.

k1/b are query parameters: they never alter N/df/avgdl. The pruning bound is
exported per query from parameter-independent envelopes under *this* k1/b and
the live corpus revision, so switching configuration can never prune with a
bound qualified for another configuration. This module is a thin wrapper over
the real ranker; the parameterised score kernel it used to carry lives in
``scorer.score_p`` and is re-exported here for callers that imported it.
"""
from ranker import query as _query
from scorer import (DEFAULT_B, DEFAULT_K1, contribution_p, idf, score_p,
                    validate_params)

# Backwards-compatible names for existing callers.
score = score_p


def query(index, terms, k, k1=DEFAULT_K1, b=DEFAULT_B):
    """Configurable query. Invalid parameters raise before any index change."""
    rows, audit = _query(index, terms, k, k1=k1, b=b)
    # Return the same (rows, skipped) shape the old configurable caller used,
    # while the full qualified audit remains available via query_audited().
    return rows, audit['skipped']


def query_audited(index, terms, k, k1=DEFAULT_K1, b=DEFAULT_B):
    return _query(index, terms, k, k1=k1, b=b)


__all__ = ['query', 'query_audited', 'score', 'contribution_p', 'idf',
           'validate_params', 'DEFAULT_K1', 'DEFAULT_B']
