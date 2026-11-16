"""Projection of the open-orders relational view.

Visible (public) columns only: order_id, customer_id, customer_name, amount.
Hidden columns (customers.private_note, orders.internal_note) and closed /
out-of-scope orders are never exported.
"""

from credential import ViewEditError, issue, normalize_scope

_PUBLIC_COLUMNS = ('order_id', 'customer_id', 'customer_name', 'amount')


def _query(db, norm_scope):
    sql = """SELECT o.order_id, o.customer_id, c.name AS customer_name, o.amount
             FROM orders o JOIN customers c USING (customer_id)
             WHERE o.status = 'open'"""
    params = ()
    if norm_scope is not None:
        # amount_lo <= amount < amount_hi -- the same half-open semantics as
        # scope_reader.read_scope's Python filter, evaluated inside SQLite.
        sql += ' AND o.amount >= ? AND o.amount < ?'
        params = (norm_scope[0], norm_scope[1])
    sql += ' ORDER BY o.order_id'
    return [dict(zip(_PUBLIC_COLUMNS, row)) for row in db.execute(sql, params)]


def read_view(db, scope=None):
    """Return public open-order rows, optionally restricted to a scope.

    ``scope=None`` is the original whole-view entry point. A scope is
    ``{'amount_lo', 'amount_hi'}`` and keeps rows with
    ``amount_lo <= amount < amount_hi``.
    """
    norm_scope = normalize_scope(scope)
    return _query(db, norm_scope)


def open_view(db, scope=None):
    """Read the view and bind the result to a saveable credential.

    Returns ``(rows, credential)``. The credential fixes the normalized scope
    plus every visible identity and original public value, and is the only
    thing :func:`writeback.put_view` needs to determine the edit boundary.
    """
    norm_scope = normalize_scope(scope)
    rows = _query(db, norm_scope)
    return rows, issue(None if norm_scope is None else
                       {'amount_lo': norm_scope[0], 'amount_hi': norm_scope[1]}, rows)
