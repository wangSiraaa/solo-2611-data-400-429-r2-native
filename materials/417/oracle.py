"""Independent full-scoring oracle.

For verification only: it never consults blocks, envelopes or maintained
counters. It reads every active raw payload, recomputes N/df/avgdl from those
payloads with the original ``scorer.statistics`` kernel, scores every active
document with the same BM25 formula the ranker uses, and sorts by the official
tie protocol. Its raw reads are metered under a separate phase so they are
never confused with query cost.
"""
from scorer import score_p, statistics, validate_params
from ranker import _tie_key, _validate_k, _validate_terms


def oracle(index, terms, k, k1=1.2, b=0.75):
    k1, b = validate_params(k1, b)
    terms = _validate_terms(terms)
    k = _validate_k(k)
    store = index.store
    store.phase = 'oracle'
    reads_start = store.document_reads['oracle']

    payloads = {}
    for doc_id in index.active_ids():
        payloads[doc_id] = store.read(doc_id)

    # Independent statistics, rebuilt from raw payloads, not from counters.
    raw_stats = statistics(payloads)
    rows = []
    for doc_id, tokens in payloads.items():
        if not any(term in tokens for term in terms):
            continue
        rows.append((doc_id, score_p(terms, tokens, raw_stats, k1, b)))
    rows.sort(key=_tie_key)
    return rows[:k], {
        'n': raw_stats['n'],
        'df': {t: raw_stats['df'].get(t, 0) for t in terms},
        'avgdl': raw_stats['avgdl'],
        'oracle_raw_document_accesses':
            store.document_reads['oracle'] - reads_start,
        'oracle_scored': len(rows),
    }
