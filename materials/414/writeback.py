"""Relational write-back for the open-orders view.

The submitted target is the *complete desired content of one view read*, not
a replacement for the two base tables. A credential from
:func:`view.open_view` anchors the edit boundary:

* order_ids visible in that read but absent from the target are deleted;
* closed orders, out-of-scope orders and hidden columns are never expressed
  by the view, so they are never deleted or overwritten;
* an order_id appearing after the read (a new open member) is not part of the
  old view either -- touching it makes the credential STALE_VIEW;
* customer names belong to the shared customer entity, so a rename follows
  every order (including closed / out-of-scope ones) but never touches
  private_note.

Everything happens in one SQLite transaction: any rejection rolls the whole
request back, so a failure leaves no partial table behind.
"""

from credential import VIEW_NAME, ViewEditError, verify

_PUBLIC_COLUMNS = ('order_id', 'customer_id', 'customer_name', 'amount')
_REQUIRED_KEYS = frozenset(_PUBLIC_COLUMNS)
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _validate_target(target):
    """Shape/type checks on the submitted rows (no database access)."""
    if not isinstance(target, list):
        raise ViewEditError('BAD_TARGET', 'target must be a list of view rows')
    rows = []
    seen_orders = set()
    for i, row in enumerate(target):
        if not isinstance(row, dict) or frozenset(row) != _REQUIRED_KEYS:
            raise ViewEditError(
                'BAD_TARGET',
                "target row %d must contain exactly the view columns %s"
                % (i, ','.join(_PUBLIC_COLUMNS)))
        order_id = row['order_id']
        customer_id = row['customer_id']
        customer_name = row['customer_name']
        amount = row['amount']
        if not all(isinstance(v, str) and v != ''
                   for v in (order_id, customer_id, customer_name)):
            raise ViewEditError(
                'BAD_TARGET',
                "target row %d has a non-string/empty order_id, customer_id or customer_name" % i)
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ViewEditError('BAD_TARGET',
                                "target row for order '%s' has a non-integer amount" % order_id)
        if not (_INT64_MIN <= amount <= _INT64_MAX):
            raise ViewEditError(
                'OUT_OF_SCOPE',
                "target order_id '%s' amount %d is outside the signed 64-bit domain"
                % (order_id, amount))
        if order_id in seen_orders:
            raise ViewEditError('CONFLICT',
                                "order_id '%s' appears more than once in the target" % order_id)
        seen_orders.add(order_id)
        rows.append({'order_id': order_id, 'customer_id': customer_id,
                     'customer_name': customer_name, 'amount': amount})
    return rows


def _in_scope(amount, scope):
    return scope is None or scope[0] <= amount < scope[1]


def put_view(db, desired, credential=None):
    """Apply one complete view target against the two base tables.

    With a credential the edit is confined to that read's scope and guarded
    against concurrent changes to visible identities/public values. Without
    one (legacy whole-view callers) the scope is the full open-orders view and
    no staleness guard is available. Raises :class:`ViewEditError` on any
    rejection; nothing is written in that case.
    """
    target = _validate_target(desired)

    if credential is not None:
        payload = verify(credential)
        scope = tuple(payload['scope']) if payload['scope'] is not None else None
        anchored = True
    else:
        payload = None
        scope = None
        anchored = False

    # Support being called inside an outer transaction: use a SAVEPOINT so a
    # rejection only undoes this request.
    in_outer = db.in_transaction
    if in_outer:
        db.execute('SAVEPOINT put_view')
    else:
        db.execute('BEGIN IMMEDIATE')
    try:
        snapshot = {}
        if anchored:
            # Re-read the *current* public tuples of exactly the identities
            # the credential saw. Snapshot order_id uniqueness is guaranteed
            # by the read, but reject a duplicate defensively.
            for order_id, old_cid, old_name, old_amount in payload['rows']:
                if order_id in snapshot:
                    raise ViewEditError('BAD_CREDENTIAL',
                                        "credential lists order_id '%s' twice" % order_id)
                cur = db.execute(
                    """SELECT o.customer_id, c.name, o.amount
                       FROM orders o JOIN customers c USING (customer_id)
                       WHERE o.order_id = ? AND o.status = 'open'""",
                    (order_id,)).fetchone()
                # Missing (deleted / turned closed) or any changed public
                # value means the source moved under this read.
                if cur is None or cur != (old_cid, old_name, old_amount):
                    raise ViewEditError(
                        'STALE_VIEW',
                        "order_id '%s' changed or left the open view since the credential was read"
                        % order_id)
                snapshot[order_id] = (old_cid, old_name, old_amount)

            # Open members that appeared after the read. Credential scope is
            # the only authority for the edit boundary -- the caller cannot
            # widen it.
            placeholders = ','.join('?' for _ in snapshot) or "''"
            newcomers = db.execute(
                """SELECT o.order_id FROM orders o
                   WHERE o.status = 'open' AND o.order_id NOT IN (%s)""" % placeholders,
                tuple(snapshot)).fetchall()
            for (order_id,) in newcomers:
                amount = db.execute('SELECT amount FROM orders WHERE order_id = ?',
                                    (order_id,)).fetchone()[0]
                if _in_scope(amount, scope):
                    raise ViewEditError(
                        'STALE_VIEW',
                        "order_id '%s' entered the credential's scope after the read; "
                        "re-read the view before writing" % order_id)

        # Every target row must still lie inside the credential's scope.
        for row in target:
            if not _in_scope(row['amount'], scope):
                label = 'whole view' if scope is None else \
                    '[%d, %d)' % (scope[0], scope[1])
                raise ViewEditError(
                    'OUT_OF_SCOPE',
                    "target order_id '%s' with amount %d is outside the credential scope %s"
                    % (row['order_id'], row['amount'], label))

        # One customer entity must carry one name across all target rows.
        target_names = {}
        for row in target:
            previous = target_names.get(row['customer_id'])
            if previous is not None and previous != row['customer_name']:
                raise ViewEditError(
                    'CONFLICT',
                    "customer_id '%s' is assigned conflicting customer_name values '%s' and '%s'"
                    % (row['customer_id'], previous, row['customer_name']))
            target_names.setdefault(row['customer_id'], row['customer_name'])

        target_by_id = {row['order_id']: row for row in target}

        # A new order_id may not borrow an identity already held by a row the
        # view does not show (a closed order or an out-of-scope open order).
        # Existing open orders visible to this read are ordinary updates.
        for order_id in target_by_id:
            if anchored and order_id in snapshot:
                continue
            hidden = db.execute(
                'SELECT status, amount FROM orders WHERE order_id = ?',
                (order_id,)).fetchone()
            if hidden is None:
                continue
            status, amount = hidden
            if status == 'closed':
                raise ViewEditError(
                    'CONFLICT',
                    "order_id '%s' is already used by a closed order and must not be reopened"
                    % order_id)
            if not anchored:
                # Full-view legacy call: every open order is visible.
                continue
            if not _in_scope(amount, scope):
                raise ViewEditError(
                    'CONFLICT',
                    "order_id '%s' already belongs to an out-of-scope open order; "
                    "its identity cannot be borrowed" % order_id)
            # In-scope open row missing from the snapshot: the freshness
            # guard above handles it, fail closed rather than hijacking.
            raise ViewEditError(
                'STALE_VIEW',
                "order_id '%s' is an open member not covered by the credential; re-read the view"
                % order_id)

        existing_customers = {cid: name for cid, name in
                              db.execute('SELECT customer_id, name FROM customers')}
        open_ids = {r[0] for r in db.execute(
            "SELECT order_id FROM orders WHERE status = 'open'")}

        # ---- customer entity changes (names only; private_note untouched) ----
        for customer_id, new_name in target_names.items():
            if customer_id in existing_customers:
                if existing_customers[customer_id] != new_name:
                    # Shared-entity rename: closed and out-of-scope orders
                    # referencing this customer display the new name too.
                    db.execute('UPDATE customers SET name = ? WHERE customer_id = ?',
                               (new_name, customer_id))
            else:
                # New customer: schema default private_note (''), not exported.
                db.execute('INSERT INTO customers(customer_id, name) VALUES (?, ?)',
                           (customer_id, new_name))

        # ---- order deletions: only identities this read actually showed ----
        if anchored:
            stale_ids = [oid for oid in snapshot if oid not in target_by_id]
            if stale_ids:
                db.executemany('DELETE FROM orders WHERE order_id = ?',
                               [(oid,) for oid in stale_ids])
        else:
            # Legacy full-view call: every existing open order is visible by
            # definition, so absent open order_ids are genuine deletions.
            stale_ids = [oid for oid in open_ids if oid not in target_by_id]
            if stale_ids:
                db.executemany('DELETE FROM orders WHERE order_id = ?',
                               [(oid,) for oid in stale_ids])

        # ---- order inserts and public-column updates ----
        for row in target:
            order_id = row['order_id']
            if anchored and order_id in snapshot:
                old_cid, _old_name, old_amount = snapshot[order_id]
                if old_cid != row['customer_id'] or old_amount != row['amount']:
                    db.execute(
                        'UPDATE orders SET customer_id = ?, amount = ? WHERE order_id = ?',
                        (row['customer_id'], row['amount'], order_id))
                # internal_note is never expressed by the view: leave as-is.
            elif not anchored and order_id in open_ids:
                current = db.execute(
                    'SELECT customer_id, amount FROM orders WHERE order_id = ?',
                    (order_id,)).fetchone()
                if current != (row['customer_id'], row['amount']):
                    db.execute(
                        'UPDATE orders SET customer_id = ?, amount = ? WHERE order_id = ?',
                        (row['customer_id'], row['amount'], order_id))
            else:
                # New order: schema default internal_note (''), status open.
                db.execute(
                    "INSERT INTO orders(order_id, customer_id, amount, status) "
                    "VALUES (?, ?, ?, 'open')",
                    (order_id, row['customer_id'], row['amount']))

        # Customers with no open orders stay; there is no garbage collection.
        if in_outer:
            db.execute('RELEASE SAVEPOINT put_view')
        else:
            db.commit()
        return {'view': VIEW_NAME,
                'scope': None if scope is None else
                        {'amount_lo': scope[0], 'amount_hi': scope[1]},
                'written': len(target), 'deleted': len(stale_ids)}
    except Exception:
        if in_outer:
            db.execute('ROLLBACK TO SAVEPOINT put_view')
            db.execute('RELEASE SAVEPOINT put_view')
        else:
            db.rollback()
        raise
