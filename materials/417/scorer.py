from collections import Counter
import math


def statistics(docs):
    n=len(docs)
    df=Counter()
    for tokens in docs.values():df.update(set(tokens))
    return dict(n=n,df=df,avgdl=sum(map(len,docs.values()))/n if n else 0.0)


def contribution(term,tokens,stats):
    tf=tokens.count(term)
    if not tf or not stats['n']:return 0.0
    idf=math.log(1+(stats['n']-stats['df'][term]+0.5)/(stats['df'][term]+0.5))
    return idf*tf*2.2/(tf+1.2*(0.25+0.75*len(tokens)/stats['avgdl']))


def score(terms,tokens,stats):
    return sum(contribution(t,tokens,stats) for t in terms)


# ---------------------------------------------------------------------------
# Parameterised kernel. k1=1.2, b=0.75 reduces exactly to contribution() above;
# that path keeps using the original function so default scores are bit-identical.
# k1/b are query-time scoring parameters only: they never change N, df or avgdl.
# ---------------------------------------------------------------------------

DEFAULT_K1 = 1.2
DEFAULT_B = 0.75


def idf(term, stats):
    """The single idf formula shared by every scoring configuration."""
    return math.log(1 + (stats['n'] - stats['df'][term] + 0.5)
                    / (stats['df'][term] + 0.5))


def contribution_p(term, tokens, stats, k1=DEFAULT_K1, b=DEFAULT_B):
    tf = tokens.count(term)
    if not tf or not stats['n']:
        return 0.0
    return idf(term, stats) * tf * (k1 + 1) / (
        tf + k1 * (1 - b + b * len(tokens) / stats['avgdl']))


def score_p(terms, tokens, stats, k1=DEFAULT_K1, b=DEFAULT_B):
    if k1 == DEFAULT_K1 and b == DEFAULT_B:
        return score(terms, tokens, stats)
    return sum(contribution_p(t, tokens, stats, k1, b) for t in terms)


def validate_params(k1=DEFAULT_K1, b=DEFAULT_B):
    """Reject non-finite or out-of-range parameters without touching the index."""
    if isinstance(k1, bool) or isinstance(b, bool):
        raise ValueError('k1/b must be finite real numbers, not booleans')
    if not isinstance(k1, (int, float)) or not isinstance(b, (int, float)):
        raise ValueError('k1/b must be finite real numbers')
    k1f, bf = float(k1), float(b)
    if not (math.isfinite(k1f) and math.isfinite(bf)):
        raise ValueError('k1/b must be finite')
    if not (0.1 <= k1f <= 3.0):
        raise ValueError('k1 must satisfy 0.1 <= k1 <= 3')
    if not (0.0 <= bf <= 1.0):
        raise ValueError('b must satisfy 0 <= b <= 1')
    return k1f, bf
