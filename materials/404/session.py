"""Incremental-relation session for the protocol in ``protocol.json``.

One :meth:`Session.pump` consumes at most one decoded envelope.  A legal
frame is applied to a private copy of the current JSON tree, materialized into
orders/lines with provenance, and published atomically to SQLite.  The
published preview therefore moves one whole frame at a time, while ``query()``
between pumps is always an observable intermediate data product.

Terminal states:

``receiving``           a legal non-final frame is published
``complete``            a legal final frame without GraphQL errors
``complete_with_errors``a legal final frame carrying GraphQL errors
``incomplete``          EOF while ``hasNext`` was still true
``failed``              protocol violation, duplicate identity or publish error

A failed frame never changes the last successfully published rows, paths or
frame numbers.  Once failed, the session stops reading the source.
"""

from __future__ import annotations

import copy
import tempfile

from frame_source import FrameSource
from incremental import (
    MAX_PATH_DEPTH,
    DuplicateIdentity,
    ProtocolError,
    apply_frame,
    frame_errors,
    reconcile,
    validate_envelope,
)
from sqlite_view import SQLiteView

MAX_FRAMES = 16

# States that should never consume another envelope.
_STOPPED = frozenset({'failed', 'incomplete'})
_TERMINAL = frozenset({'complete', 'complete_with_errors'})


class Session:
    def __init__(self, path, db_path=None):
        self._temp = tempfile.TemporaryDirectory() if db_path is None else None
        self.db_path = (
            str(db_path) if db_path is not None
            else self._temp.name + '/preview.sqlite'
        )
        self.source = FrameSource(path)
        self.view = SQLiteView(self.db_path)
        self.view.reset()

        self.publish_hook = None
        self.status = 'new'
        self.frame = 0                 # last published data frame
        self.frames_read = 0           # envelopes consumed from the source
        self.errors = []               # accumulated GraphQL errors
        self.session_error = None      # protocol/publication failure detail
        self.last_stats = {}           # derivation cost of the last frame
        self._tree = None              # current JSON document
        self._orders = {}              # business id -> provenance record
        self._lines = {}               # (order id, line id) -> record
        self._stopped = False

    # ------------------------------------------------------------------ API
    def query(self):
        """Read the currently published preview through SQLite."""
        return self.view.query()

    def pump(self):
        """Consume at most one envelope and publish the resulting frame."""
        if self._stopped:
            return self._after_stop()

        envelope = self.source.read()
        if envelope is None:
            return self._finish_incomplete()

        self.frames_read += 1
        first = self.frames_read == 1

        try:
            if self.frames_read > MAX_FRAMES:
                raise ProtocolError('more than %d envelopes' % MAX_FRAMES)
            validate_envelope(envelope, first=first)
            tree = self._next_tree(envelope, first)
            stats = {}
            orders, lines = reconcile(
                self._orders, self._lines, tree, self.frame + 1, stats)
            status, errors = self._frame_status(envelope)
            preview = {
                'status': status,
                'last_frame': self.frame + 1,
                'errors': errors,
                'orders': [
                    {'id': order_id, **record}
                    for order_id, record in orders.items()
                ],
                'lines': [
                    {'order_id': key[0], 'id': key[1], **record}
                    for key, record in lines.items()
                ],
            }
            self.view.publish(preview, self.publish_hook)
        except Exception as exc:  # OSError, ProtocolError, sqlite3.Error
            self._fail(exc)
            return self.query()

        # Commit succeeded: the new frame is now the published state.
        self.frame += 1
        self._tree = tree
        self._orders = orders
        self._lines = lines
        self.errors = errors
        self.last_stats = stats
        self.status = status
        if status in _TERMINAL:
            self._stopped = True
        return self.query()

    # ------------------------------------------------------------- internals
    def _after_stop(self):
        """A pump after a terminal state.

        ``hasNext=false`` closes the response.  Attempting another pump when
        the source really delivers one more envelope is a protocol error;
        an attempt with no further envelope just returns the terminal preview.
        A failed or incomplete session never consumes anything further.
        """
        if self.status in _TERMINAL and self.source.read() is not None:
            self._fail(ProtocolError('frame after terminal response'))
        return self.query()

    def _next_tree(self, envelope, first):
        if first:
            return copy.deepcopy(envelope.get('data'))
        return apply_frame(self._tree, envelope)

    def _frame_status(self, envelope):
        """Resolve a legal frame's status and accumulated errors."""
        errors = self.errors + frame_errors(envelope)
        if envelope['hasNext']:
            return 'receiving', errors
        return ('complete_with_errors' if errors else 'complete'), errors

    def _finish_incomplete(self):
        """EOF while a hasNext=true frame was still current."""
        if not self._stopped:
            self._stopped = True
            if self.status not in _TERMINAL:
                self.status = 'incomplete'
                self.view.set_status('incomplete', self.errors, self.frame)
        return self.query()

    def _fail(self, exc):
        self._stopped = True
        self.status = 'failed'
        if isinstance(exc, DuplicateIdentity):
            kind = 'duplicate_identity'
        elif isinstance(exc, ProtocolError):
            kind = 'protocol_error'
        else:
            kind = 'publish_error'
        self.session_error = {
            'message': str(exc) or exc.__class__.__name__,
            'type': kind,
        }
        errors = self.errors + [self.session_error]
        try:
            self.view.set_status('failed', errors, self.frame)
        except Exception:
            # A broken database is itself the failure; the in-memory status
            # still stops this session from consuming further frames.
            self.errors = errors
            return
        self.errors = errors
