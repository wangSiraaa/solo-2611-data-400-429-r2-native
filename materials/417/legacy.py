"""The original buggy pattern, kept verbatim in spirit for the demo.

This is NOT the live engine. It reproduces the defect the task starts from:
numeric bounds are computed once at build time from the statistics of that
moment and reused after deletions/replacements even though N, df and avgdl
have changed. Its purpose is to make the counterexample observable next to the
repaired engine; nothing in the query path of the real index uses this code.
"""
from bounds import build_bound
from scorer import statistics


class LegacyIndex:
    def __init__(self, docs, block_size=1):
        self.docs = {k: list(v) for k, v in docs.items()}
        ids = sorted(self.docs)
        self.blocks = [ids[i:i + block_size]
                       for i in range(0, len(ids), block_size)]
        # Bounds frozen against build-time statistics -- the stale-bound bug.
        frozen = statistics(self.docs)
        self.frozen_stats = frozen
        self.bounds = [build_bound(block, self.docs, frozen)
                       for block in self.blocks]

    def remove(self, rid):
        del self.docs[rid]          # statistics/bounds never refreshed

    def put(self, rid, tokens):
        fresh = rid not in self.docs
        self.docs[rid] = list(tokens)
        if fresh:
            self.blocks.append([rid])
            self.bounds.append(
                build_bound([rid], self.docs, statistics(self.docs)))


def legacy_query(index, terms, k):
    """Original pruning loop: recomputes stats by scanning payloads, yet still
    prunes with the frozen build-time bounds."""
    stats = statistics(index.docs)   # full payload scan, like the old ranker
    candidates, skipped, scored = [], [], 0
    for number, (block, bounds) in enumerate(
            zip(index.blocks, index.bounds)):
        threshold = candidates[k - 1][1] if len(candidates) >= k else -1
        upper = sum(bounds.get(term, 0.0) for term in terms)
        if upper < threshold:
            skipped.append(number)
            continue
        for rid in block:
            if rid not in index.docs:
                continue
            tokens = index.docs[rid]
            if not any(term in tokens for term in terms):
                continue
            from scorer import score
            scored += 1
            candidates.append((rid, score(terms, tokens, stats)))
        candidates.sort(key=lambda pair: (-pair[1], pair[0].encode()))
    return candidates[:k], dict(skipped=skipped, scored=scored,
                                frozen_revision_stats=dict(
                                    n=index.frozen_stats['n'],
                                    avgdl=index.frozen_stats['avgdl']))
