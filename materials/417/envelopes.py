"""Parameter-independent block envelopes and query-time bound export.

A block envelope stores, per term that is *currently active* in the block,
two statistics only:

    tf_max   = maximum within-document term frequency in the block
    len_min  = minimum document length among the block docs containing the term

Neither number depends on idf, avgdl, k1 or b, so an envelope never goes stale
because corpus statistics or scoring parameters changed. It only goes stale
when the tf/length/active-membership facts it was built from change, which is
exactly when the index rebuilds it (during build/put/remove).

At query time a provable numeric upper bound is exported:

    contrib(idf, tf, L) = idf * tf*(k1+1) / (tf + k1*(1-b+b*L/avgdl))

For fixed idf, k1, b, avgdl the fraction tf*(k1+1)/(tf + k1*(...)) is
increasing in tf (derivative positive) and, when b>0, decreasing in L, because
the denominator grows with L. Hence using (tf_max, len_min) -- which may come
from different documents, but only loosens the bound -- dominates the
contribution of every actual (tf, L) pair in the block. idf is non-negative for
every df in 0..N (the log argument is >= 1), so factoring it out is safe.
With b=0 length drops out of the formula entirely.

The exported bound additionally carries a small floating-point slack and the
full applicability basis (revision, N, df, avgdl, k1, b); a bound produced for
one corpus revision or parameter set is never silently reused for another.
"""
import math

from scorer import idf

# Explicit rounding slack: every skip proof is "upper < threshold"; the slack
# absorbs representational rounding while never masking a real gap. It does not
# rescue a bound that is structurally invalid.
ROUND_SLACK = 1e-12


def build_envelope(block_doc_ids, doc_lengths, doc_tfs):
    """Rebuild one block envelope from index metadata only.

    doc_lengths: {doc_id: length}; doc_tfs: {doc_id: {term: tf}}
    No full token payload is read here: tf tables and lengths are metadata.
    """
    envelope = {}
    for doc_id in block_doc_ids:
        length = doc_lengths[doc_id]
        for term, tf in doc_tfs[doc_id].items():
            if tf <= 0:
                continue
            cur = envelope.get(term)
            if cur is None:
                # Independent extremes: tf_max and len_min may come from
                # different documents; combining them only loosens the bound.
                envelope[term] = [tf, length]
            else:
                if tf > cur[0]:
                    cur[0] = tf
                if length < cur[1]:
                    cur[1] = length
    return envelope


def term_upper_bound(term, envelope, stats, k1, b):
    """Export a safe numeric upper bound for one term over one block.

    Returns 0.0 when the term has no active occurrence in the block.
    """
    entry = envelope.get(term)
    if entry is None:
        return 0.0
    tf_max, len_min = entry
    if stats['n'] == 0 or term not in stats['df'] or stats['avgdl'] <= 0:
        return 0.0
    denom = tf_max + k1 * (1.0 - b + b * len_min / stats['avgdl'])
    if denom <= 0.0:
        return 0.0
    frac = tf_max * (k1 + 1.0) / denom
    bound = idf(term, stats) * frac
    # Guard against rounding noise below zero; add slack upward only.
    if bound <= 0.0:
        return 0.0
    return bound * (1.0 + ROUND_SLACK) + ROUND_SLACK


def block_upper_bound(terms, envelope, stats, k1, b):
    """Sum of per-term bounds: every document score is such a sum, so the sum
    of per-term maxima dominates every document in the block."""
    return sum(term_upper_bound(t, envelope, stats, k1, b) for t in terms)


def bound_basis(term_bounds, stats, k1, b, revision):
    """Serializable qualification record attached to every bound use."""
    return {
        'revision': revision,
        'k1': k1,
        'b': b,
        'n': stats['n'],
        'avgdl': stats['avgdl'],
        'df': {t: stats['df'].get(t, 0) for t in term_bounds},
        'term_bounds': dict(term_bounds),
        'envelope_kind': 'parameter_independent_tfmax_lenmin',
        'round_slack': ROUND_SLACK,
    }
