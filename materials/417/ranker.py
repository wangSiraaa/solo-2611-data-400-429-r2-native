from scorer import score,statistics


def query(index,terms,k):
    stats=statistics(index.docs)
    candidates=[]
    skipped=[]
    scored=0
    for number,(block,bounds) in enumerate(zip(index.blocks,index.bounds)):
        threshold=candidates[k-1][1] if len(candidates)>=k else -1
        upper=sum(bounds.get(term,0.0) for term in terms)
        if upper<threshold:
            skipped.append(number)
            continue
        for rid in block:
            if rid not in index.docs:continue
            tokens=index.docs[rid]
            if not any(term in tokens for term in terms):continue
            scored+=1
            candidates.append((rid,score(terms,tokens,stats)))
        candidates.sort(key=lambda pair:(-pair[1],pair[0].encode()))
    return candidates[:k],dict(skipped=skipped,scored=scored)
