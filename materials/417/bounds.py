from scorer import contribution


def build_bound(doc_ids,docs,stats):
    terms={term for rid in doc_ids for term in docs[rid]}
    return {term:max(contribution(term,docs[rid],stats) for rid in doc_ids) for term in terms}
