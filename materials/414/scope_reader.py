"""Scoped read entry point.

Delegates to :func:`view.read_view` so that reading, credential verification
and the write-back diff all share one definition of the scope predicate
(``amount_lo <= amount < amount_hi``) and one projection of public columns.
"""

from view import read_view


def read_scope(db, scope):
    return read_view(db, scope)
