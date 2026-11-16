"""Metered raw-payload storage.

A *raw document access* is one read of a document's complete token payload.
Repeated reads -- including in-memory cache hits -- are counted repeatedly.
Per-document tf/length metadata and postings live outside this store and are
metered separately by the query layer.

Persistence uses a directory with two JSON files:
    payloads.json  -- doc_id -> full token list (the raw payloads)
    index.json     -- postings, statistics, envelopes, audit history
"""
import json
import os
from collections import Counter


class JsonPayloadStore:
    def __init__(self, path=None):
        self.path = path
        self._payloads = {}
        self.phase = 'unclassified'
        # Raw-document read accounting (the query-budget metric).
        self.document_reads = Counter()
        self.token_reads = Counter()
        # Write accounting (build/update cost, never charged to a query).
        self.document_writes = Counter()
        self.token_writes = Counter()

    # ------------------------------------------------------------- payload IO
    def read(self, doc_id):
        tokens = self._payloads[doc_id]
        # Counted on every read; an in-memory hit is still a payload access.
        self.document_reads[self.phase] += 1
        self.token_reads[self.phase] += len(tokens)
        return tokens

    def write(self, doc_id, tokens):
        self._payloads[doc_id] = list(tokens)
        self.document_writes[self.phase] += 1
        self.token_writes[self.phase] += len(tokens)

    def delete(self, doc_id):
        del self._payloads[doc_id]

    def contains(self, doc_id):
        # Membership is metadata, not a payload decode.
        return doc_id in self._payloads

    def ids(self):
        return sorted(self._payloads)

    # ------------------------------------------------------------- accounting
    @property
    def reads_total(self):
        return sum(self.document_reads.values())

    @property
    def writes_total(self):
        return sum(self.document_writes.values())

    @property
    def access_total(self):
        return self.reads_total + self.writes_total

    def reset_counters(self, phase=None):
        self.document_reads = Counter()
        self.token_reads = Counter()
        self.document_writes = Counter()
        self.token_writes = Counter()
        if phase is not None:
            self.phase = phase

    def read_account(self):
        return {
            'phase': self.phase,
            'raw_document_reads': dict(self.document_reads),
            'raw_token_reads': dict(self.token_reads),
            'raw_document_reads_total': self.reads_total,
            'payload_writes_total': self.writes_total,
        }

    # ---------------------------------------------------------- persistence
    def save_index(self, state, path):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, 'payloads.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(self._payloads, handle, ensure_ascii=False)
        with open(os.path.join(path, 'index.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False)
        self.path = path

    def load_index(self, path):
        with open(os.path.join(path, 'payloads.json'), 'r',
                  encoding='utf-8') as handle:
            self._payloads = json.load(handle)
        with open(os.path.join(path, 'index.json'), 'r',
                  encoding='utf-8') as handle:
            state = json.load(handle)
        self.path = path
        self.reset_counters()
        return state

    def load_payloads(self, path):
        with open(os.path.join(path, 'payloads.json'), 'r',
                  encoding='utf-8') as handle:
            self._payloads = json.load(handle)
        self.path = path
        self.reset_counters()
