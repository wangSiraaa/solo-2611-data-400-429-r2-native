"""Raw-document access accounting for a real query.

A raw-document access is one read of a document's complete token payload;
repeated reads count repeatedly and in-memory cache hits still count. This
probe reports, for the same public 100-document selective query:

* the repaired engine's per-query raw payload reads (statistics come from
  maintained counters, so only genuinely scored payloads are read);
* the metadata accesses (counters, envelopes, tf/length tables) separately;
* what the legacy full-scan pattern would cost when statistics and bounds are
  rebuilt inside the query, proving a renamed scan is still a scan.
"""
import json
from pathlib import Path

from inverted import Index
from legacy import LegacyIndex, legacy_query
from oracle import oracle
from ranker import query
from scorer import statistics


class CountedDocuments(dict):
    """Legacy-style instrumentation: every payload decode is counted.

    Retained to demonstrate the old statistics(index.docs) cost; membership
    and scalar metadata do not decode a payload, values/items iteration does.
    """

    def __init__(self, documents):
        super().__init__(documents)
        self.phase = 'unclassified'
        self.document_accesses = __import__('collections').Counter()
        self.token_accesses = __import__('collections').Counter()

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
    n_docs = len(case['docs'])

    # ---- Repaired engine: maintained statistics, no in-query payload scan --
    index = Index(case['docs'], case['block_size'])
    index.store.reset_counters(phase='query')
    result, audit = query(index, case['terms'], case['k'])
    repaired_reads = audit['raw_document_accesses']

    # Independent oracle on the same state for correctness cross-check.
    oracle_rows, oracle_info = oracle(index, case['terms'], case['k'])

    # ---- Legacy pattern: statistics rebuilt by scanning every payload ------
    legacy = LegacyIndex(case['docs'], case['block_size'])
    counted = CountedDocuments(legacy.docs)
    counted.phase = 'query'
    legacy_stats = statistics(counted)          # 2 raw reads per doc here
    stats_scan_reads = counted.document_accesses['query']
    legacy_rows, legacy_audit = legacy_query(legacy, case['terms'], case['k'])

    return {
        'scope': 'Same public 100-doc selective query; query window starts at '
                 'query entry, so update/build reads are excluded.',
        'result': result,
        'oracle_result': oracle_rows,
        'matches_oracle': result == oracle_rows,
        'repaired_engine': {
            'scored': audit['scored'],
            'raw_document_accesses': repaired_reads,
            'raw_token_accesses': audit['raw_token_accesses'],
            'statistics_source': 'maintained N/df/total_length counters; '
                                 'avgdl derived without touching payloads',
            'metadata_accesses_reported_separately': audit['metadata_accesses'],
            'skipped_nonempty_blocks': audit['skipped_nonempty_blocks'],
            'budget_exclusive': 50,
            'under_budget': repaired_reads < 50 and audit['scored'] < 50,
        },
        'legacy_full_scan_pattern': {
            'in_query_statistics_scan_raw_reads': stats_scan_reads,
            'expected_scan_reads': 2 * n_docs,
            'plus_scoring_reads': legacy_audit['scored'],
            'total_query_raw_reads': stats_scan_reads + legacy_audit['scored'],
            'observation': 'statistics(docs) traverses all 100 payloads twice '
                           'inside the query; low scored alone is not low '
                           'source work, and renaming the scan does not '
                           'remove the accesses.',
        },
        'accounting_rule': 'raw payload reads (incl. cache hits) vs metadata '
                           '(counters/envelopes/postings tf/length) are '
                           'reported on separate lines; metadata never encodes '
                           'a full token payload.',
    }


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
