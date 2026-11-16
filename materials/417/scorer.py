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
