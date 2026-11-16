"""Updatable BM25 inverted index with current statistics and qualified bounds.

Design summary
--------------
* Raw token payloads live in a separate payload store (``store.py``), never in
  the posting metadata. Reading one payload counts as one raw-document access;
  repeated reads count repeatedly, including cache hits.
* Corpus statistics (N, per-term df, total length) are maintained incrementally
  on build/put/remove from per-document metadata (length + tf table), so a
  query never scans payloads to recompute N/df/avgdl.
* Blocks and block envelopes (parameter-independent tf_max/len_min, see
  ``envelopes.py``) are fixed per block membership and rebuilt only for the
  blocks touched by an update.
* Every statistics/envelope version carries a monotonically increasing
  ``revision`` and an applicability basis; query-time numeric bounds are
  exported from envelopes under the *current* revision and *this query's*
  k1/b, so stale idf/avgdl numbers can never justify a skip.
"""
import json
import os

from envelopes import build_envelope
from store import JsonPayloadStore

MAX_DOCS = 1000
MAX_TOKENS_PER_DOC = 200
MAX_BLOCKS = 1000
MAX_UPDATES = 200


class Index:
    def __init__(self, docs=None, block_size=1, store_path=None, store=None):
        if not isinstance(block_size, int) or not (1 <= block_size <= 32):
            raise ValueError('block_size must be an integer in 1..32')
        self.block_size = block_size
        # Callers reopening an index pass a store that already holds payloads;
        # otherwise a fresh metered store is created.
        self.store = store if store is not None else JsonPayloadStore(
            store_path)
        # Per-document metadata, independent of raw payloads.
        self.doc_length = {}        # doc_id -> len(tokens)
        self.doc_tfs = {}           # doc_id -> {term: tf}
        # Inverted lists: term -> {doc_id: tf}; postings carry tf/length only.
        self.postings = {}
        # Fixed blocks: list of doc_id lists; ids removed physically on rebuild.
        self.blocks = []
        # One parameter-independent envelope per block.
        self.envelopes = []
        # Incremental corpus statistics.
        self.n = 0
        self.df = {}
        self.total_length = 0
        self.revision = 0
        self.update_count = 0       # puts + removes after the initial build
        self.history = []           # audit trail of build/update operations
        self.query_log = []         # audit trail of executed queries
        if docs is not None:
            self._build(docs)

    def _check_limits(self, doc_id, tokens):
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError('doc_id must be a non-empty string')
        if len(tokens) > MAX_TOKENS_PER_DOC:
            raise ValueError(
                f'document {doc_id!r} has {len(tokens)} tokens; limit is '
                f'{MAX_TOKENS_PER_DOC}')
        if self.n + 1 > MAX_DOCS:
            raise ValueError(f'corpus limit is {MAX_DOCS} active documents')
        if len(self.blocks) >= MAX_BLOCKS and (
                not self.blocks
                or len(self.blocks[-1]) >= self.block_size):
            raise ValueError(f'block limit is {MAX_BLOCKS}')
        if self.update_count >= MAX_UPDATES:
            raise ValueError(f'update limit is {MAX_UPDATES}')

    # ------------------------------------------------------------------ build
    def _build(self, docs):
        ids = sorted(docs)
        if len(ids) > MAX_DOCS:
            raise ValueError(f'corpus limit is {MAX_DOCS} documents')
        for doc_id in ids:
            tokens = list(docs[doc_id])
            if len(tokens) > MAX_TOKENS_PER_DOC:
                raise ValueError(
                    f'document {doc_id!r} has {len(tokens)} tokens; limit is '
                    f'{MAX_TOKENS_PER_DOC}')
        self.blocks = [ids[i:i + self.block_size]
                       for i in range(0, len(ids), self.block_size)]
        if len(self.blocks) > MAX_BLOCKS:
            raise ValueError(
                f'{len(self.blocks)} blocks exceed the limit of {MAX_BLOCKS}')
        for doc_id in ids:
            tokens = list(docs[doc_id])
            self.store.write(doc_id, tokens)
            self._install_metadata(doc_id, tokens)
        self.envelopes = [
            build_envelope(block, self.doc_length, self.doc_tfs)
            for block in self.blocks]
        self.revision += 1
        self.history.append({
            'op': 'build', 'revision': self.revision, 'n': self.n,
            'total_length': self.total_length,
            'block_count': len(self.blocks),
            'raw_accesses_build': self.store.access_total,
        })

    def _install_metadata(self, doc_id, tokens):
        """Derive length/tf/posting/statistic entries for one new document."""
        tfs = {}
        for term in tokens:
            tfs[term] = tfs.get(term, 0) + 1
        self.doc_length[doc_id] = len(tokens)
        self.doc_tfs[doc_id] = tfs
        self.n += 1
        self.total_length += len(tokens)
        for term, tf in tfs.items():
            self.postings.setdefault(term, {})[doc_id] = tf
            self.df[term] = self.df.get(term, 0) + 1

    def _uninstall_metadata(self, doc_id):
        tfs = self.doc_tfs.pop(doc_id)
        length = self.doc_length.pop(doc_id)
        self.n -= 1
        self.total_length -= length
        for term in tfs:
            posting = self.postings[term]
            del posting[doc_id]
            if not posting:
                del self.postings[term]
                del self.df[term]
            else:
                self.df[term] -= 1

    # ---------------------------------------------------------------- updates
    def put(self, doc_id, tokens):
        """Full replacement of one document (or insertion)."""
        tokens = list(tokens)
        fresh = doc_id not in self.doc_length
        if fresh:
            self._check_limits(doc_id, tokens)
        elif len(tokens) > MAX_TOKENS_PER_DOC:
            raise ValueError(
                f'document {doc_id!r} has {len(tokens)} tokens; limit is '
                f'{MAX_TOKENS_PER_DOC}')
        if self.update_count >= MAX_UPDATES:
            raise ValueError(f'update limit is {MAX_UPDATES}')
        access_before = self.store.access_total
        if fresh:
            block_no = self._place_fresh_doc(doc_id)
        else:
            block_no = self._block_of(doc_id)
            self._uninstall_metadata(doc_id)
            self.store.delete(doc_id)
        self.store.write(doc_id, tokens)
        self._install_metadata(doc_id, tokens)
        self._rebuild_block(block_no)
        self.revision += 1
        self.update_count += 1
        self.history.append({
            'op': 'put', 'revision': self.revision, 'doc_id': doc_id,
            'block': block_no, 'fresh': fresh, 'n': self.n,
            'total_length': self.total_length,
            'raw_accesses_update': self.store.access_total - access_before,
        })
        return block_no

    def remove(self, doc_id):
        if doc_id not in self.doc_length:
            raise KeyError(doc_id)
        if self.update_count >= MAX_UPDATES:
            raise ValueError(f'update limit is {MAX_UPDATES}')
        access_before = self.store.access_total
        block_no = self._block_of(doc_id)
        self._uninstall_metadata(doc_id)
        self.store.delete(doc_id)
        self._rebuild_block(block_no)
        self.revision += 1
        self.update_count += 1
        self.history.append({
            'op': 'remove', 'revision': self.revision, 'doc_id': doc_id,
            'block': block_no, 'n': self.n,
            'total_length': self.total_length,
            'raw_accesses_update': self.store.access_total - access_before,
        })
        return block_no

    def _place_fresh_doc(self, doc_id):
        """Pack into the last block while it has room; else open a new one.

        Block ids are sorted once per block at build; a later insertion appends
        to that block's list and the block is re-sorted, keeping per-block
        UTF-8 doc_id order deterministic without renumbering any existing block.
        """
        if self.blocks and len(self.blocks[-1]) < self.block_size:
            block_no = len(self.blocks) - 1
            self.blocks[block_no].append(doc_id)
            self.blocks[block_no].sort(key=lambda rid: rid.encode('utf-8'))
        else:
            block_no = len(self.blocks)
            self.blocks.append([doc_id])
            self.envelopes.append({})
        return block_no

    def _block_of(self, doc_id):
        for number, block in enumerate(self.blocks):
            if doc_id in block:
                return number
        raise KeyError(doc_id)

    def _rebuild_block(self, block_no):
        """Drop inactive ids from the block and rebuild its envelope.

        Envelopes are rebuilt from tf/length metadata only: no raw payload
        access is needed. Empty blocks are retained as empty slots so block
        numbers stay stable for audit records.
        """
        block = [rid for rid in self.blocks[block_no]
                 if rid in self.doc_length]
        self.blocks[block_no] = block
        self.envelopes[block_no] = build_envelope(
            block, self.doc_length, self.doc_tfs)

    # ------------------------------------------------------------- statistics
    def current_stats(self):
        """N/df/avgdl from maintained counters; no payload is read."""
        avgdl = self.total_length / self.n if self.n else 0.0
        return {'n': self.n, 'df': dict(self.df), 'avgdl': avgdl,
                'total_length': self.total_length, 'revision': self.revision}

    def tokens_of(self, doc_id):
        """Read one full raw payload (metered)."""
        return self.store.read(doc_id)

    def active_ids(self):
        return sorted(self.doc_length)

    # ------------------------------------------------------------- persistence
    def save(self, path=None):
        target = self.store.path if path is None else path
        if target is None:
            raise ValueError('no store path configured')
        self.store.save_index(self._persist_state(), target)
        return target

    @classmethod
    def open(cls, path):
        store = JsonPayloadStore(path)
        state = store.load_index(path)
        # Reuse the store that already holds the loaded payloads; do not let
        # the constructor replace it with a fresh empty one.
        index = cls(block_size=state['block_size'], store=store)
        index.doc_length = state['doc_length']
        index.doc_tfs = state['doc_tfs']
        index.postings = {t: {rid: tf for rid, tf in posting.items()}
                          for t, posting in state['postings'].items()}
        index.blocks = state['blocks']
        index.envelopes = [
            {t: tuple(entry) for t, entry in env.items()}
            for env in state['envelopes']]
        index.n = state['n']
        index.df = state['df']
        index.total_length = state['total_length']
        index.revision = state['revision']
        index.history = state.get('history', [])
        index.update_count = state.get('update_count', 0)
        index.query_log = state.get('query_log', [])
        return index

    def _persist_state(self):
        return {
            'block_size': self.block_size,
            'doc_length': self.doc_length,
            'doc_tfs': self.doc_tfs,
            'postings': self.postings,
            'blocks': self.blocks,
            'envelopes': [dict(env) for env in self.envelopes],
            'n': self.n,
            'df': self.df,
            'total_length': self.total_length,
            'revision': self.revision,
            'update_count': self.update_count,
            'history': self.history,
            'query_log': self.query_log,
        }
