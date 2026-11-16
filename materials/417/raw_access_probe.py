"""Count genuine raw-document accesses, including statistics/bound rebuild work.

This authoring instrument wraps the real source store. It is not a BM25 index.
Repeated accesses count repeatedly. Membership and stored scalar metadata do
not decode a raw document; values/items iteration does.
"""
import json
from collections import Counter
from pathlib import Path

from inverted import Index
from ranker import query


class CountedDocuments(dict):
    def __init__(self, documents):
        super().__init__(documents)
        self.phase = 'unclassified'
        self.document_accesses = Counter()
        self.token_accesses = Counter()

    def __getitem__(self, identity):
        tokens = super().__getitem__(identity)
        self.document_accesses[self.phase] += 1
        self.token_accesses[self.phase] += len(tokens)
        return tokens

    def values(self):
        for identity in self:
            yield self[identity]

    def items(self):
        for identity in self:
            yield identity, self[identity]


def run():
    case = json.loads(Path(__file__).with_name('benchmark.json').read_text())
    index = Index(case['docs'], case['block_size'])
    index.docs = CountedDocuments(index.docs)
    index.docs.phase = 'query'
    result, audit = query(index, case['terms'], case['k'])
    reads = index.docs.document_accesses['query']
    assert reads == 2 * len(case['docs']) + audit['scored']
    assert reads >= 50
    return {
        'scope': 'Actual existing static query only; counts its statistics rebuild too.',
        'result': result,
        'baseline_reported_scored': audit['scored'],
        'actual_query_raw_document_accesses': reads,
        'actual_query_token_accesses': index.docs.token_accesses['query'],
        'required_raw_document_accesses_exclusive': 50,
        'budget_passed': reads < 50,
        'observation': 'The old statistics(docs) traverses all 100 raw documents twice during query; low scored alone is not low source work.',
    }


if __name__ == '__main__':
    print(json.dumps(run()))
