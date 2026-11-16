"""Existing configurable caller: scoring parameters change but bound qualification does not."""
import math
from scorer import statistics


def score(terms, tokens, stats, k1=1.2, b=0.75):
    result = 0.0
    for term in terms:
        tf = tokens.count(term)
        if not tf or not stats['n']:
            continue
        idf = math.log(1 + (stats['n'] - stats['df'][term] + 0.5) / (stats['df'][term] + 0.5))
        result += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(tokens) / stats['avgdl']))
    return result


def query(index, terms, k, k1=1.2, b=0.75):
    stats = statistics(index.docs)
    candidates, skipped = [], []
    for number, (block, bounds) in enumerate(zip(index.blocks, index.bounds)):
        threshold = candidates[k - 1][1] if len(candidates) >= k else -1
        if sum(bounds.get(term, 0.0) for term in terms) < threshold:
            skipped.append(number)
            continue
        for identity in block:
            if identity not in index.docs:
                continue
            tokens = index.docs[identity]
            if any(term in tokens for term in terms):
                candidates.append((identity, score(terms, tokens, stats, k1, b)))
        candidates.sort(key=lambda pair: (-pair[1], pair[0].encode()))
    return candidates[:k], skipped
