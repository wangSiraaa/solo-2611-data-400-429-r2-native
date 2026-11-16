"""Query-time ranking with provable, qualified block pruning.

Protocol for every query:

1. Fix one complete index state: the current revision plus N/df/total_length
   from maintained counters (no payload scan; avgdl derived arithmetically).
2. For each block, export a numeric upper bound *now*, from the block's
   parameter-independent envelope, the fixed current statistics and this
   query's k1/b. The bound record carries the full applicability basis.
3. Skip a block only when its exported bound is strictly below the current
   k-th candidate score. Equality is never a skip (a smaller doc_id on a tie
   must be allowed to overtake). Every skip is recorded with its proof.
4. Docs in retained blocks are checked through tf metadata; only docs hitting
   at least one query term pay a raw-payload read and a real BM25 score.

Metadata accesses (envelopes, postings, tf/length tables, counters) are
metered separately from raw-payload reads.
"""
from collections import Counter

from envelopes import term_upper_bound
from scorer import score_p, validate_params


def _validate_terms(terms):
    if not isinstance(terms, (list, tuple)) or not (1 <= len(terms) <= 4):
        raise ValueError('query needs 1..4 terms')
    if any(not isinstance(t, str) for t in terms):
        raise ValueError('terms must be strings')
    if len(set(terms)) != len(terms):
        raise ValueError('query terms must be distinct')
    return list(terms)


def _validate_k(k):
    if isinstance(k, bool) or not isinstance(k, int) or not (1 <= k <= 20):
        raise ValueError('k must be an integer in 1..20')
    return k


def _tie_key(pair):
    return (-pair[1], pair[0].encode('utf-8'))


def query(index, terms, k, k1=1.2, b=0.75):
    """Return (rows, audit). rows = [(doc_id, score), ...] up to k."""
    k1, b = validate_params(k1, b)
    terms = _validate_terms(terms)
    k = _validate_k(k)

    # Step 1: fix the index state. Maintained counters -> zero payload reads.
    stats = index.current_stats()
    store = index.store
    store.phase = 'query'
    # Snapshot so each query reports only its own reads even though the
    # underlying per-phase counters accumulate across the index's lifetime.
    reads_start = store.document_reads['query']
    tokens_start = store.token_reads['query']
    metadata = Counter()

    candidates = []
    block_audit = []
    skipped = []

    for number, (block, envelope) in enumerate(
            zip(index.blocks, index.envelopes)):
        # Step 2: export this block's bound under current stats + parameters.
        metadata['envelope_blocks_inspected'] += 1
        per_term = {}
        for term in terms:
            per_term[term] = term_upper_bound(term, envelope, stats, k1, b)
            if term in envelope:
                metadata['envelope_term_entries_read'] += 1
        upper = sum(per_term.values())
        threshold = candidates[k - 1][1] if len(candidates) >= k else None

        basis = {
            'block': number,
            'active_docs_in_block': len(block),
            'term_bounds': per_term,
            'block_upper_bound': upper,
            'threshold': threshold,
            'basis_revision': stats['revision'],
            'basis_n': stats['n'],
            'basis_avgdl': stats['avgdl'],
            'basis_df': {t: stats['df'].get(t, 0) for t in terms},
            'basis_k1': k1,
            'basis_b': b,
        }

        # Step 3: strict inequality only; ties are never pruned.
        if threshold is not None and upper < threshold:
            basis['decision'] = 'skipped'
            basis['proof'] = (
                f'exported_upper={upper!r} < kth_score={threshold!r} under '
                f'revision={stats["revision"]}, N={stats["n"]}, '
                f'avgdl={stats["avgdl"]}, k1={k1}, b={b}; '
                'no document in this block can enter top-k')
            skipped.append(number)
            block_audit.append(basis)
            continue

        basis['decision'] = 'read'
        if threshold is None:
            basis['reason'] = 'fewer than k candidates seen yet'
        else:
            basis['reason'] = 'bound >= threshold (tie-safe: not pruned)'

        # Step 4: metadata pre-filter, then payload reads for real hits only.
        scored_in_block = 0
        for doc_id in block:
            tfs = index.doc_tfs[doc_id]              # metadata, not payload
            metadata['doc_tf_metadata_reads'] += 1
            if not any(term in tfs for term in terms):
                continue
            tokens = store.read(doc_id)              # one raw-payload access
            score = score_p(terms, tokens, stats, k1, b)
            candidates.append((doc_id, score))
            candidates.sort(key=_tie_key)
            scored_in_block += 1
        basis['scored_in_block'] = scored_in_block
        block_audit.append(basis)

    rows = candidates[:k]
    raw_reads = store.document_reads['query'] - reads_start
    token_reads = store.token_reads['query'] - tokens_start
    audit = {
        'revision': stats['revision'],
        'statistics_used': {
            'n': stats['n'],
            'df': {t: stats['df'].get(t, 0) for t in terms},
            'avgdl': stats['avgdl'],
            'total_length': stats['total_length'],
        },
        'parameters': {'k1': k1, 'b': b},
        'k': k,
        'terms': terms,
        'skipped': skipped,
        'skipped_nonempty_blocks': sum(
            1 for number in skipped
            if block_audit[number]['active_docs_in_block'] > 0),
        'scored': len(candidates),
        'raw_document_accesses': raw_reads,
        'raw_token_accesses': token_reads,
        'metadata_accesses': dict(metadata),
        'block_decisions': block_audit,
        'bound_kind': 'query-time export from parameter-independent '
                      '(tf_max, len_min) envelopes; current revision + params',
    }
    index.query_log.append({
        'revision': stats['revision'],
        'terms': terms, 'k': k, 'parameters': {'k1': k1, 'b': b},
        'statistics_used': audit['statistics_used'],
        'rows': rows,
        'skipped': skipped,
        'skipped_nonempty_blocks': audit['skipped_nonempty_blocks'],
        'scored': len(candidates),
        'raw_document_accesses': raw_reads,
        'metadata_accesses': dict(metadata),
    })
    return rows, audit
