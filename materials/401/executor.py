"""Streaming LEFT JOIN with a runtime nested-loop -> hash physical switch.

Public contract (unchanged): ``Executor(path).next()`` yields at most one output
row per call, and ``inspect()`` returns a snapshot.  ``main.py`` remains the
entry point.

Why the starter was wrong
-------------------------
1. It fed *emitted pairs* to the planner as a proxy for scan work.  In the
   sparse fixture almost no pairs are emitted even though every left row still
   pays for a full right scan, so the planner never switched.
2. On switching it replayed ``seen + remaining left iterator`` through a fresh
   index, re-emitting pairs that had already been handed to the caller and
   re-running left rows whose scans were already paid for.

Handoff choice: **row-boundary handoff** (not mid-row frontier migration)
========================================================================
The planner is consulted exactly at a left-row boundary: after the current left
row's right scan has matched or been exhausted, and *before* the next left row
is processed.  State ownership at that instant is explicit:

* published pairs      -- already returned by ``next()``; never touched again.
* finished left row    -- match flag final, accounting closed.  If it matched,
                          all its matching right rows were already published.
                          If not, its single NULL extension is emitted right at
                          the boundary.  It is never re-scanned.
* unconsumed left rows -- still in the one-shot ``Sources.left`` iterator;
                          exactly the rows the hash path will consume next.
* right index          -- built by one full immutable right scan at the
                          boundary; from then on only this index is probed.

This costs one full right build scan, but needs no saved per-row match cursor
and no replay of published pairs or of the completed left row.  The alternative
*frontier migration* (switch mid-row, save the right cursor) saves at most one
partial scan but must carry "how many of this row's matches are already
published" and "did this row match at all" across the operator change; the row
boundary makes both facts final before switching.

Telemetry
---------
``Sources.audit`` counts actual API row visits only.  Every right-row visit --
nested probing, the hash build, discarded partial scans and rescans -- bumps
``right_reads``.  In-memory index work is reported separately as
``index_lookups`` (one per left key lookup) and ``index_probes`` (one per
candidate right row returned by the index); it is never folded into source
reads.
"""

from sources import Sources
from planner import Planner


_SENTINEL = object()
_CONTINUE = object()


def matches(left, right):
    return (
        left['key'] is not None
        and right['key'] is not None
        and left['key'] == right['key']
    )


def pair(left, right):
    return {
        'left_id': left['id'],
        'right_id': None if right is None else right['id'],
        'left_value': left['value'],
        'right_value': None if right is None else right['value'],
    }


class Executor:
    def __init__(self, path):
        self.sources = Sources(path)
        self.planner = Planner(self.sources.estimate)
        self.closed = False

        # One-shot left stream; never reopened, never replayed.
        self._left_iter = iter(self.sources.left())
        self._left_exhausted = False
        self._pending_left = None  # peeked, not yet processed left row

        # Nested-loop cursor for the current left row.
        self._cur_left = None
        self._matched = False
        self._scan_rows = 0
        self._right_iter = None

        # Strategy and right-side index.
        self._strategy = 'nested_loop'
        self._index = None
        self._index_built = False
        self._build_right_reads = 0
        self._switch_after_completed = None
        self._right_reads_at_switch = None

        # Hash-mode cursor for the current left row.
        self._hash_matches = None
        self._hash_pos = 0

        # Real observations (not estimates).
        self._completed_lefts = 0
        self._matched_lefts = 0
        self._unmatched_lefts = 0
        self._pairs_emitted = 0

        # In-memory index work, counted apart from source row visits.
        self.index_lookups = 0
        self.index_probes = 0

    # ------------------------------------------------------------------ API
    def next(self):
        if self.closed:
            return None
        while True:
            result = (
                self._hash_next()
                if self._strategy == 'hash'
                else self._nested_next()
            )
            if result is _CONTINUE:
                continue
            if result is None:
                self.closed = True
                return None
            self._pairs_emitted += 1
            return result

    def inspect(self):
        audit = dict(self.sources.audit)
        build = self._build_right_reads
        return {
            'source': audit,
            'strategy': self._strategy,
            'switch': {
                'switched': bool(self.planner.switched),
                'trigger': 'left_row_boundary',
                'handoff': 'row_boundary',
                'after_completed_left_rows': self._switch_after_completed,
                'index_built': self._index_built,
                'build_source_right_reads': build,
                'right_reads_before_switch': self._right_reads_at_switch,
            },
            'probes': {
                'index_lookups': self.index_lookups,
                'index_probes': self.index_probes,
                'note': 'in-memory index work; not source row visits',
            },
            'reconcile': {
                # Source-side accounting: nested scan visits predate the build
                # scan; together they must equal the audited total.  When no
                # switch happened every read belongs to nested scans.
                'nested_right_reads': (
                    audit['right_reads'] if self._right_reads_at_switch is None
                    else self._right_reads_at_switch
                ),
                'build_right_reads': build,
                'total_right_reads': audit['right_reads'],
                'source_reads_match': (
                    audit['right_reads']
                    == (audit['right_reads']
                        if self._right_reads_at_switch is None
                        else self._right_reads_at_switch + build)
                ),
                # Index-side accounting: every candidate returned by a lookup
                # is at most one per right occurrence, and only equals the
                # published match count plus any empty lookups.
                'left_lookups_after_switch': self.index_lookups,
                'right_candidates_probed': self.index_probes,
                'pairs_emitted': self._pairs_emitted,
            },
            'state': {
                'left_rows_completed': self._completed_lefts,
                'matched_left_rows': self._matched_lefts,
                'unmatched_left_rows': self._unmatched_lefts,
                'pairs_emitted': self._pairs_emitted,
                'current_left_open': self._cur_left is not None,
                'left_source_exhausted': self._left_exhausted,
            },
            'decisions': list(self.planner.decisions),
            'complete': self.closed,
        }

    # --------------------------------------------------------------- nested
    def _nested_next(self):
        if self._cur_left is None:
            # First row of the nested phase (or after a boundary handoff that
            # returned without starting a row -- but hash mode owns that case).
            left = self._take_pending_or_pull()
            if left is None:
                return None
            self._start_nested_row(left)

        rec = next(self._right_iter, _SENTINEL)
        if rec is not _SENTINEL:
            self._scan_rows += 1  # real source right-row visit
            if matches(self._cur_left, rec):
                self._matched = True
                return pair(self._cur_left, rec)
            return _CONTINUE

        # Current left row exhausted: its match state is final.
        left = self._cur_left
        was_matched = self._matched
        last_scan = self._scan_rows
        self._right_iter = None
        self._cur_left = None
        self._completed_lefts += 1
        if was_matched:
            self._matched_lefts += 1
        else:
            self._unmatched_lefts += 1

        # Peek the next left row so the decision knows whether work remains.
        nxt = self._take_pending_or_pull()
        chosen = self._consult_planner(last_scan, more_left=nxt is not None)

        if chosen == 'hash':
            self._switch_after_completed = self._completed_lefts
            self._right_reads_at_switch = self.sources.audit['right_reads']
            self._build_index()
            self._strategy = 'hash'
            if nxt is not None:
                self._pending_left = nxt
            # This left row is already final: emit its NULL extension if it
            # had no match, otherwise move straight into hash mode.  Published
            # pairs are not replayed and the finished row is not re-scanned.
            return _CONTINUE if was_matched else pair(left, None)

        # Stay nested-loop.
        if nxt is not None:
            self._start_nested_row(nxt)
        return _CONTINUE if was_matched else pair(left, None)

    def _start_nested_row(self, left):
        self._cur_left = left
        self._matched = False
        self._scan_rows = 0
        self._right_iter = iter(self.sources.right())

    def _take_pending_or_pull(self):
        if self._pending_left is not None:
            left = self._pending_left
            self._pending_left = None
            return left
        if self._left_exhausted:
            return None
        try:
            return next(self._left_iter)
        except StopIteration:
            self._left_exhausted = True
            return None

    def _consult_planner(self, last_scan, more_left):
        observation = {
            'left_rows_completed': self._completed_lefts,
            'more_left': more_left,
            'right_reads': self.sources.audit['right_reads'],
            'right_rows_last_scan': last_scan,
            'pairs_emitted': self._pairs_emitted,
            'matched_left_rows': self._matched_lefts,
            'unmatched_left_rows': self._unmatched_lefts,
        }
        return self.planner.observe(observation)

    # ----------------------------------------------------------------- hash
    def _build_index(self):
        index = {}
        reads = 0
        for item in self.sources.right():
            reads += 1  # real source right-row visit (second full pass)
            key = item['key']
            if key is not None:
                index.setdefault(key, []).append(item)
        self._index = index
        self._index_built = True
        self._build_right_reads = reads
        self._right_iter = None

    def _hash_next(self):
        if self._cur_left is None:
            left = self._take_pending_or_pull()
            if left is None:
                return None
            self._cur_left = left
            self._matched = False
            key = left['key']
            if key is None:
                # NULL never matches; no index lookup is performed at all.
                self._hash_matches = []
            else:
                self.index_lookups += 1
                self._hash_matches = list(self._index.get(key, []))
            self._hash_pos = 0

        if self._hash_pos < len(self._hash_matches):
            rec = self._hash_matches[self._hash_pos]
            self._hash_pos += 1
            self.index_probes += 1
            self._matched = True
            return pair(self._cur_left, rec)

        # Left row finished in hash mode.
        left = self._cur_left
        was_matched = self._matched
        self._cur_left = None
        self._hash_matches = None
        self._completed_lefts += 1
        if was_matched:
            self._matched_lefts += 1
        else:
            self._unmatched_lefts += 1
        return _CONTINUE if was_matched else pair(left, None)
