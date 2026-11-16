"""Saveable view credentials for the open-orders relational view.

A credential fixes the *identity* of a read: the normalized scope, the
visible member identities and the original public values at read time.
It is an opaque, HMAC-signed token (never executable SQL, never contains
private_note/internal_note) and lets :mod:`writeback` distinguish a real
deletion (visible at read time, absent from the target) from data that the
view simply could not express (closed orders, out-of-scope rows, hidden
columns).
"""

import base64
import hashlib
import hmac
import json
import os
import threading

# Process-local signing secret. Credentials only need to be unforgeable and
# opaque to API callers; they are not meant to survive a process restart.
_LOCK = threading.Lock()
_SECRET = os.urandom(32)

VIEW_NAME = 'open_orders_v1'
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


class ViewEditError(Exception):
    """Rejected write-back. ``code`` is one of the documented error codes."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def reset_signing_key():
    """Rotate the in-process signing key (mainly for tests)."""
    global _SECRET
    with _LOCK:
        _SECRET = os.urandom(32)


def normalize_scope(scope):
    """Validate and return the canonical (amount_lo, amount_hi) tuple.

    ``None`` means the whole open-orders view (unbounded scope). Endpoints
    are signed 64-bit integers and must satisfy ``lo < hi``.
    """
    if scope is None:
        return None
    if not isinstance(scope, dict) or set(scope) != {'amount_lo', 'amount_hi'}:
        raise ViewEditError('BAD_SCOPE',
                            "scope must be {'amount_lo','amount_hi'} or be omitted")
    lo, hi = scope['amount_lo'], scope['amount_hi']
    if isinstance(lo, bool) or isinstance(hi, bool) or \
            not isinstance(lo, int) or not isinstance(hi, int):
        raise ViewEditError('BAD_SCOPE', 'scope endpoints must be integers')
    if not (_INT64_MIN <= lo <= _INT64_MAX and _INT64_MIN <= hi <= _INT64_MAX):
        raise ViewEditError('OUT_OF_SCOPE', 'scope endpoints must be signed 64-bit integers')
    if lo >= hi:
        raise ViewEditError('BAD_SCOPE', 'scope requires amount_lo < amount_hi')
    return lo, hi


def _b64encode(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64decode(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def issue(scope, rows):
    """Mint a credential for the rows produced by one scoped read.

    ``rows`` are public view rows (order_id, customer_id, customer_name,
    amount) ordered by order_id.
    """
    norm = normalize_scope(scope)
    payload = {
        'view': VIEW_NAME,
        'scope': None if norm is None else list(norm),
        # Snapshot of every public tuple visible at read time.
        'rows': [[r['order_id'], r['customer_id'], r['customer_name'], r['amount']]
                 for r in rows],
    }
    body = _b64encode(json.dumps(payload, separators=(',', ':'),
                                 sort_keys=True).encode('utf-8'))
    with _LOCK:
        sig = hmac.new(_SECRET, body.encode('ascii'), hashlib.sha256).digest()
    return body + '.' + _b64encode(sig)


def verify(token):
    """Validate signature/shape and return the decoded credential payload.

    Raises :class:`ViewEditError` with code ``BAD_CREDENTIAL`` on any
    malformed, tampered or foreign token. The payload is plain data; nothing
    here (or anywhere else) is ever fed to SQLite as SQL.
    """
    if not isinstance(token, str) or token.count('.') != 1:
        raise ViewEditError('BAD_CREDENTIAL', 'credential must be the token returned by open_view')
    body, sig_text = token.split('.')
    try:
        given_sig = _b64decode(sig_text)
    except Exception:
        raise ViewEditError('BAD_CREDENTIAL', 'credential signature is malformed')
    with _LOCK:
        expected_sig = hmac.new(_SECRET, body.encode('ascii'), hashlib.sha256).digest()
    if not hmac.compare_digest(given_sig, expected_sig):
        raise ViewEditError('BAD_CREDENTIAL', 'credential signature does not match any read of this view')
    try:
        payload = json.loads(_b64decode(body).decode('utf-8'))
    except Exception:
        raise ViewEditError('BAD_CREDENTIAL', 'credential body is malformed')
    if not isinstance(payload, dict) or payload.get('view') != VIEW_NAME \
            or not isinstance(payload.get('rows'), list) \
            or set(payload) != {'view', 'scope', 'rows'}:
        raise ViewEditError('BAD_CREDENTIAL', 'credential does not identify view %s' % VIEW_NAME)
    scope = payload['scope']
    if scope is not None:
        if not (isinstance(scope, list) and len(scope) == 2):
            raise ViewEditError('BAD_CREDENTIAL', 'credential scope is malformed')
        # Re-check the bound relation captured at read time.
        if not (isinstance(scope[0], int) and isinstance(scope[1], int)
                and scope[0] < scope[1]):
            raise ViewEditError('BAD_CREDENTIAL', 'credential scope is malformed')
    for row in payload['rows']:
        if not (isinstance(row, list) and len(row) == 4
                and isinstance(row[0], str) and isinstance(row[1], str)
                and isinstance(row[2], str) and isinstance(row[3], int)
                and not isinstance(row[3], bool)):
            raise ViewEditError('BAD_CREDENTIAL', 'credential snapshot row is malformed')
    return payload
