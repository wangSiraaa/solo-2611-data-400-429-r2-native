"""End-to-end verification of the relational view edit interface.

Every scenario runs against a real SQLite database rebuilt from the shipped
fixtures. Successful puts are checked by querying *all* base-table columns
and then re-reading through the credential's bound scope; failed puts are
checked against the logical dump and, for file databases, the exact on-disk
bytes of the database file.
"""

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from credential import VIEW_NAME, ViewEditError, _b64decode
from scope_reader import read_scope
from view import open_view, read_view
from writeback import put_view

ROOT = Path(__file__).parent
FIX = json.loads((ROOT / 'fixtures.json').read_text())
SCOPED = json.loads((ROOT / 'scoped-cases.json').read_text())
PASSED = []
FAILED = []


def check(name, cond, detail=''):
    (PASSED if cond else FAILED).append(name)
    print(('  ok ' if cond else 'FAIL ') + name + ((' -- ' + detail) if detail and not cond else ''))
    assert cond, name


def fresh_db(seed, path=None):
    if path is None:
        fd, path = tempfile.mkstemp(suffix='.db')
        Path(path).unlink()  # sqlite creates it
    db = sqlite3.connect(path)
    db.executescript((ROOT / 'schema.sql').read_text())
    db.executemany('INSERT INTO customers VALUES (?,?,?)',
                   [(c['customer_id'], c['name'], c['private_note']) for c in seed['customers']])
    db.executemany('INSERT INTO orders VALUES (?,?,?,?,?)',
                   [(o['order_id'], o['customer_id'], o['amount'], o['status'], o['internal_note'])
                    for o in seed['orders']])
    db.commit()
    return db, path


def dump(db):
    return {
        'customers': sorted(db.execute('SELECT customer_id,name,private_note FROM customers')),
        'orders': sorted(db.execute('SELECT order_id,customer_id,amount,status,internal_note FROM orders')),
    }


def expect_error(name, code, fn, db, hidden_tokens):
    before = dump(db)
    db.commit()  # flush any pending pages so the snapshot is stable
    db.execute('PRAGMA wal_checkpoint(FULL)')
    file_before = Path(_current_path).read_bytes() if _current_path else None
    try:
        fn()
    except ViewEditError as e:
        check(name + ' [' + code + ']', e.code == code, e.code + ': ' + e.message)
        leaked = [t for t in hidden_tokens if t in e.message]
        check(name + ' hides private/internal notes from error text', not leaked, str(leaked))
    else:
        check(name + ' [' + code + ']', False, 'no error raised')
    check(name + ' leaves logical source content unchanged', dump(db) == before)
    if file_before is not None:
        # rollback already ran inside put_view; flush leftover journal pages
        db.commit()
        db.execute('PRAGMA wal_checkpoint(FULL)')
        same = Path(_current_path).read_bytes() == file_before
        check(name + ' leaves database file bytes unchanged', same)
    return db


_current_path = None


def section(title):
    print('\n== ' + title + ' ==')


# ---------------------------------------------------------------- full view
seed = {'customers': FIX['customers'], 'orders': FIX['orders']}
HIDDEN = ['keep-a', 'keep-b', 'keep-1', 'keep-2', 'keep-3', 'protected', 'archive']

section('full view: empty edit (GetPut) and single amount edit')
db, p = fresh_db(seed)
_current_path = p
original = dump(db)
rows, token = open_view(db)
check('read_view without scope equals open_view rows', read_view(db) == rows)
check('view exposes only the four public columns',
      all(set(r) == {'order_id', 'customer_id', 'customer_name', 'amount'} for r in rows))
put_view(db, rows, token)
check('empty edit preserves every base row and hidden column', dump(db) == original)
reread, _ = open_view(db)
check('empty edit re-read equals target', reread == rows)

rows2, token2 = open_view(db)
rows2[0]['amount'] = 11  # open-a: 10 -> 11
put_view(db, rows2, token2)
oa = db.execute("SELECT order_id,customer_id,amount,status,internal_note FROM orders WHERE order_id='open-a'").fetchone()
check('amount edit updates only amount', oa == ('open-a', 'c1', 11, 'open', 'keep-1'))
check('amount edit leaves other rows untouched', dump(db)['orders'] == sorted(
    [('open-a', 'c1', 11, 'open', 'keep-1'), ('open-b', 'c1', 20, 'open', 'keep-2'),
     ('closed', 'c2', 30, 'closed', 'keep-3')]))
check('amount edit re-read equals target', open_view(db)[0] == rows2)
db.close()

section('full view: shared customer rename, consistent and conflicting')
db, p = fresh_db(seed); _current_path = p
rows, token = open_view(db)
for r in rows:
    if r['customer_id'] == 'c1':
        r['customer_name'] = 'Alice X'
put_view(db, rows, token)
check('consistent rename updates the customer entity once',
      db.execute("SELECT name,private_note FROM customers WHERE customer_id='c1'").fetchone()
      == ('Alice X', 'keep-a'))
check('rename propagates through every order incl. closed references',
      dict(db.execute('SELECT o.order_id,c.name FROM orders o JOIN customers c USING(customer_id)'))
      == {'open-a': 'Alice X', 'open-b': 'Alice X', 'closed': 'Bob'})
check('rename does not touch orders of other customers or notes',
      db.execute("SELECT private_note FROM customers WHERE customer_id='c2'").fetchone() == ('keep-b',))

rows, token = open_view(db)
names = FIX['conflicting_target_names']['c1']
i = 0
for r in rows:
    if r['customer_id'] == 'c1':
        r['customer_name'] = names[i]; i += 1
db = expect_error('conflicting shared rename', 'CONFLICT',
                  lambda: put_view(db, rows, token), db, HIDDEN)
db.close()

section('full view: reassign order to existing and to brand-new customer')
db, p = fresh_db(seed); _current_path = p
rows, token = open_view(db)
for r in rows:
    if r['order_id'] == 'open-b':
        r['customer_id'] = 'c2'  # existing Bob
put_view(db, rows, token)
check('reassign keeps target customer private_note',
      db.execute("SELECT private_note FROM customers WHERE customer_id='c2'").fetchone() == ('keep-b',))
check('reassign changes only customer_id, keeps amount/status/internal_note',
      db.execute("SELECT customer_id,amount,status,internal_note FROM orders WHERE order_id='open-b'").fetchone()
      == ('c2', 20, 'open', 'keep-2'))

rows, token = open_view(db)
for r in rows:
    if r['order_id'] == 'open-a':
        r['customer_id'], r['customer_name'] = 'c3', 'Carol'
put_view(db, rows, token)
check('new customer created with schema default private_note',
      db.execute("SELECT customer_id,name,private_note FROM customers WHERE customer_id='c3'").fetchone()
      == ('c3', 'Carol', ''))
check('reassigned order moves to new customer',
      db.execute("SELECT customer_id FROM orders WHERE order_id='open-a'").fetchone() == ('c3',))
check('old customer retained while still referenced',
      db.execute("SELECT 1 FROM customers WHERE customer_id='c1'").fetchone() is not None)
db.close()

section('full view: visible deletion, retention of customers and closed orders')
db, p = fresh_db(seed); _current_path = p
rows, token = open_view(db)
target = [r for r in rows if r['order_id'] != 'open-b']
put_view(db, target, token)
check('deleted visible order is gone',
      db.execute("SELECT 1 FROM orders WHERE order_id='open-b'").fetchone() is None)
check('closed order survives visible deletion',
      db.execute("SELECT internal_note FROM orders WHERE order_id='closed'").fetchone() == ('keep-3',))
check('customers with remaining orders are retained',
      sorted(x[0] for x in db.execute('SELECT customer_id FROM customers')) == ['c1', 'c2'])
rows, token = open_view(db)
put_view(db, [], token)  # delete every visible order
check('all open orders deleted', db.execute("SELECT COUNT(*) FROM orders WHERE status='open'").fetchone() == (0,))
check('closed order still present',
      db.execute("SELECT 1 FROM orders WHERE order_id='closed'").fetchone() is not None)
check('customers without open orders are NOT garbage collected',
      sorted(x[0] for x in db.execute('SELECT customer_id FROM customers')) == ['c1', 'c2'])
db.close()

section('full view: hidden order_id cannot be borrowed; hidden changes do not stale')
db, p = fresh_db(seed); _current_path = p
rows, token = open_view(db)
bad = rows + [{'order_id': 'closed', 'customer_id': 'c1', 'customer_name': 'Alice', 'amount': 5}]
db = expect_error('new order_id occupied by closed order', 'CONFLICT',
                  lambda: put_view(db, bad, token), db, HIDDEN)

rows, token = open_view(db)
db.execute(FIX['read_then_unrelated_hidden_change'])  # internal_note of closed row
db.commit()
put_view(db, rows, token)
check('unrelated internal_note edit is preserved after put',
      db.execute("SELECT internal_note FROM orders WHERE order_id='closed'").fetchone()
      == ('new-hidden',))
check('view rows still match the untouched target', open_view(db)[0] == rows)

rows, token = open_view(db)
db.execute("INSERT INTO orders VALUES ('late-closed','c2',40,'closed','later')")
db.commit()
put_view(db, rows, token)
check('new unrelated closed order is preserved',
      db.execute("SELECT status,internal_note FROM orders WHERE order_id='late-closed'").fetchone()
      == ('closed', 'later'))
db.close()

section('full view: STALE_VIEW on visible public value / membership changes')
db, p = fresh_db(seed); _current_path = p
rows, token = open_view(db)
db.execute("UPDATE orders SET amount=11 WHERE order_id='open-a'"); db.commit()
db = expect_error('concurrent public amount edit', 'STALE_VIEW',
                  lambda: put_view(db, rows, token), db, HIDDEN)

rows, token = open_view(db)
db.execute("UPDATE orders SET status='closed' WHERE order_id='open-a'"); db.commit()
db = expect_error('visible member turned closed', 'STALE_VIEW',
                  lambda: put_view(db, rows, token), db, HIDDEN)

rows, token = open_view(db)
db.execute("INSERT INTO orders VALUES ('late-open','c2',40,'open','x')"); db.commit()
db = expect_error('new open member appears after read', 'STALE_VIEW',
                  lambda: put_view(db, rows, token), db, HIDDEN + ['x'])
db.close()

section('full view: credential and target validation; legacy entry point')
db, p = fresh_db(seed); _current_path = p
rows, token = open_view(db)
db = expect_error('tampered credential rejected', 'BAD_CREDENTIAL',
                  lambda: put_view(db, rows, token + 'x'), db, HIDDEN)
db = expect_error('foreign/garbage credential rejected', 'BAD_CREDENTIAL',
                  lambda: put_view(db, rows, 'not-a-token'), db, HIDDEN)
dup = rows + [dict(rows[0])]
db = expect_error('duplicate order_id in target', 'CONFLICT',
                  lambda: put_view(db, dup, token), db, HIDDEN)
malformed = [{'order_id': 'open-a', 'customer_id': 'c1'}]
db = expect_error('malformed target row rejected', 'BAD_TARGET',
                  lambda: put_view(db, malformed, token), db, HIDDEN)
bad_amount = [dict(r, amount='10') for r in rows]
db = expect_error('non-integer amount rejected', 'BAD_TARGET',
                  lambda: put_view(db, bad_amount, token), db, HIDDEN)

before = dump(db)
put_view(db, rows)  # legacy: no credential, whole view
check('legacy no-credential call still works and keeps hidden data', dump(db) == before)
check('legacy call re-read equals target', open_view(db)[0] == rows)
db.close()

# --------------------------------------------------------------- scoped view
sseed = {
    'customers': [{'customer_id': c[0], 'name': c[1], 'private_note': c[2]}
                  for c in SCOPED['customers']],
    'orders': [{'order_id': o[0], 'customer_id': o[1], 'amount': o[2],
                'status': o[3], 'internal_note': o[4]} for o in SCOPED['orders']],
}
scope = SCOPED['scope']
SHIDDEN = ['in-note', 'out-note', 'closed-note', 'secret-one', 'secret-two']

section('scoped view: read semantics (lo <= amount < hi)')
db, p = fresh_db(sseed); _current_path = p
check('scoped read shows only in-scope open order',
      [r['order_id'] for r in read_scope(db, scope)] == ['inside'])
check('read_view and read_scope share semantics',
      read_view(db, scope) == read_scope(db, scope))
tiny = {'customers': [{'customer_id': 'x', 'name': 'X', 'private_note': 's'}],
        'orders': [{'order_id': 'lo', 'customer_id': 'x', 'amount': 0, 'status': 'open', 'internal_note': 'n'},
                   {'order_id': 'mid', 'customer_id': 'x', 'amount': 5, 'status': 'open', 'internal_note': 'n'},
                   {'order_id': 'hi', 'customer_id': 'x', 'amount': 10, 'status': 'open', 'internal_note': 'n'}]}
tdb, tp = fresh_db(tiny); _current_path = tp
got = [r['order_id'] for r in read_view(tdb, {'amount_lo': 0, 'amount_hi': 10})]
check('half-open range includes lo boundary, excludes hi boundary', got == ['lo', 'mid'])
for bad_scope, code in (({'amount_lo': 5, 'amount_hi': 5}, 'BAD_SCOPE'),
                        ({'amount_lo': 9, 'amount_hi': 1}, 'BAD_SCOPE'),
                        ({'amount_lo': '0', 'amount_hi': 1}, 'BAD_SCOPE'),
                        ({'amount_lo': -(1 << 63) - 1, 'amount_hi': 0}, 'OUT_OF_SCOPE'),
                        ({'amount_lo': 0, 'amount_hi': (1 << 63)}, 'OUT_OF_SCOPE')):
    try:
        open_view(tdb, bad_scope); check('scope validation ' + str(bad_scope), False)
    except ViewEditError as e:
        check('scope validation ' + str(bad_scope) + ' -> ' + code, e.code == code, e.code)
tdb.close()

section('scoped view: empty edit, in-scope edit and GetPut')
db, p = fresh_db(sseed); _current_path = p
before = dump(db)
rows, token = open_view(db, scope)
check('credential binds normalized scope',
      json.loads(_b64decode(token.split('.')[0]))['scope'] == [0, 100])
put_view(db, rows, token)
check('scoped empty edit preserves all rows and hidden columns', dump(db) == before)
check('bound-scope re-read equals target', open_view(db, scope)[0] == rows)
check('whole-view read still lists out-of-scope open order',
      [r['order_id'] for r in read_view(db)] == ['inside', 'outside'])

rows, token = open_view(db, scope)
rows[0]['amount'] = 90
put_view(db, rows, token)
check('in-scope amount edit applied, internal_note kept',
      db.execute("SELECT amount,internal_note FROM orders WHERE order_id='inside'").fetchone()
      == (90, 'in-note'))
check('out-of-scope order completely untouched',
      db.execute("SELECT amount,status,internal_note FROM orders WHERE order_id='outside'").fetchone()
      == (200, 'open', 'out-note'))
db.close()

section('scoped view: OUT_OF_SCOPE target and credential cannot widen scope')
db, p = fresh_db(sseed); _current_path = p
rows, token = open_view(db, scope)
rows[0]['amount'] = SCOPED['out_of_scope_target_amount']  # 200 >= hi
db = expect_error('target row outside credential scope', 'OUT_OF_SCOPE',
                  lambda: put_view(db, rows, token), db, SHIDDEN)
# There is no scope parameter on put_view: a caller can only edit what the
# credential saw. A target trying to sneak an out-of-scope identity in is
# likewise rejected.
rows, token = open_view(db, scope)
sneak = rows + [{'order_id': 'outside', 'customer_id': 'c1',
                 'customer_name': 'shared', 'amount': 50}]
db = expect_error('borrowing out-of-scope open identity rejected', 'CONFLICT',
                  lambda: put_view(db, sneak, token), db, SHIDDEN)
db.close()

section('scoped view: what changes stale a credential and what does not')
db, p = fresh_db(sseed); _current_path = p
rows, token = open_view(db, scope)
db.execute("UPDATE orders SET amount=300 WHERE order_id='outside'"); db.commit()
put_view(db, rows, token)
check('out-of-scope amount edit staying out-of-scope does not stale',
      db.execute('SELECT amount FROM orders WHERE order_id=?', ('outside',)).fetchone() == (300,))
check('in-scope target still applied on top', open_view(db, scope)[0] == rows)

rows, token = open_view(db, scope)
db.execute("UPDATE orders SET internal_note='changed' WHERE order_id='outside'"); db.commit()
put_view(db, rows, token)
check('out-of-scope internal_note edit is preserved',
      db.execute('SELECT internal_note FROM orders WHERE order_id=?', ('outside',)).fetchone()
      == ('changed',))
db.close()

db, p = fresh_db(sseed); _current_path = p
rows, token = open_view(db, scope)
db.execute("INSERT INTO orders VALUES ('far','c2',500,'open','far-note')"); db.commit()
put_view(db, rows, token)
check('new open order born out-of-scope is retained, not stale',
      db.execute("SELECT status,internal_note FROM orders WHERE order_id='far'").fetchone()
      == ('open', 'far-note'))
db.close()

db, p = fresh_db(sseed); _current_path = p
rows, token = open_view(db, scope)
db.execute("UPDATE orders SET amount=? WHERE order_id='outside'",
           (SCOPED['membership_entering_amount'],)); db.commit()  # 200 -> 50, enters scope
db = expect_error('order entering scope after read invalidates credential', 'STALE_VIEW',
                  lambda: put_view(db, rows, token), db, SHIDDEN)
db.close()

db, p = fresh_db(sseed); _current_path = p
rows, token = open_view(db, scope)
db.execute("UPDATE orders SET amount=200 WHERE order_id='inside'"); db.commit()  # leaves scope
db = expect_error('order leaving scope after read invalidates credential', 'STALE_VIEW',
                  lambda: put_view(db, rows, token), db, SHIDDEN)
db.close()

db, p = fresh_db(sseed); _current_path = p
rows, token = open_view(db, scope)
db.execute("INSERT INTO orders VALUES ('intruder','c2',50,'open','i-note')"); db.commit()
db = expect_error('new in-scope open member invalidates credential', 'STALE_VIEW',
                  lambda: put_view(db, rows, token), db, SHIDDEN + ['i-note'])
db.close()

db, p = fresh_db(sseed); _current_path = p
rows, token = open_view(db, scope)
db.execute("UPDATE customers SET name='renamed' WHERE customer_id='c1'"); db.commit()
db = expect_error('concurrent customer rename changes public value -> stale', 'STALE_VIEW',
                  lambda: put_view(db, rows, token), db, SHIDDEN)
db.close()

section('scoped view: shared rename propagation without out-of-scope writes')
db, p = fresh_db(sseed); _current_path = p
before_out = db.execute("SELECT amount,status,internal_note FROM orders WHERE order_id='outside'").fetchone()
rows, token = open_view(db, scope)
rows[0]['customer_name'] = 'renamed-shared'
put_view(db, rows, token)
names = dict(db.execute('SELECT o.order_id,c.name FROM orders o JOIN customers c USING(customer_id)'))
check('rename reaches out-of-scope order through shared customer entity',
      names == {'inside': 'renamed-shared', 'outside': 'renamed-shared', 'closed': 'other'})
check('out-of-scope order business columns untouched',
      db.execute("SELECT amount,status,internal_note FROM orders WHERE order_id='outside'").fetchone()
      == before_out)
check('customer private_note untouched by rename',
      db.execute("SELECT private_note FROM customers WHERE customer_id='c1'").fetchone()
      == ('secret-one',))
check('closed order keeps identity and note',
      db.execute("SELECT customer_id,internal_note FROM orders WHERE order_id='closed'").fetchone()
      == ('c2', 'closed-note'))
db.close()

section('scoped view: visible delete confined to scope; two-client interleaving')
db, p = fresh_db(sseed); _current_path = p
rows, token = open_view(db, scope)
put_view(db, [], token)
check('scoped delete removes only the in-scope order',
      sorted(x[0] for x in db.execute('SELECT order_id FROM orders')) == ['closed', 'outside'])
check('all customers retained', sorted(x[0] for x in db.execute('SELECT customer_id FROM customers'))
      == ['c1', 'c2'])
db.close()

# Two real connections to one file database, controlled interleaving.
db1, p = fresh_db(sseed); _current_path = p
db2 = sqlite3.connect(p)
rows_a, token_a = open_view(db1, scope)
# Client B commits an unrelated hidden change first; A's write must survive.
db2.execute("UPDATE orders SET internal_note='b-hidden' WHERE order_id='closed'")
db2.commit()
put_view(db1, rows_a, token_a)
check('client A write succeeds after unrelated client B commit',
      db2.execute("SELECT internal_note FROM orders WHERE order_id='closed'").fetchone()
      == ('b-hidden',))
# Now B changes a public value A's credential saw -> A is stale.
rows_a2, token_a2 = open_view(db1, scope)
db2.execute("UPDATE orders SET amount=21 WHERE order_id='inside'")
db2.commit()
try:
    put_view(db1, rows_a2, token_a2)
    check('client A rejected after conflicting client B commit', False)
except ViewEditError as e:
    check('client A rejected after conflicting client B commit -> STALE_VIEW', e.code == 'STALE_VIEW')
check('client B edit remains the only committed change',
      db1.execute('SELECT amount FROM orders WHERE order_id=?', ('inside',)).fetchone() == (21,))
# A re-reads and its fresh empty edit goes through, keeping B's hidden edit.
fresh_rows, fresh_token = open_view(db1, scope)
put_view(db1, fresh_rows, fresh_token)
check('fresh credential after re-read succeeds and preserves B hidden edit',
      db1.execute("SELECT internal_note FROM orders WHERE order_id='closed'").fetchone()
      == ('b-hidden',))
db1.close(); db2.close()

section('full view: new-customer name conflict, int64 bounds and FK atomicity')
db, p = fresh_db(seed); _current_path = p
rows, token = open_view(db)
rows.append({'order_id': 'n1', 'customer_id': 'c9', 'customer_name': 'Nine', 'amount': 5})
rows.append({'order_id': 'n2', 'customer_id': 'c9', 'customer_name': 'Niner', 'amount': 6})
db = expect_error('conflicting names for one new customer', 'CONFLICT',
                  lambda: put_view(db, rows, token), db, HIDDEN)
check('neither new customer nor order half-created',
      db.execute("SELECT 1 FROM customers WHERE customer_id='c9'").fetchone() is None
      and db.execute("SELECT 1 FROM orders WHERE order_id IN ('n1','n2')").fetchone() is None)

rows, token = open_view(db)
rows.append({'order_id': 'big', 'customer_id': 'c1', 'customer_name': 'Alice',
             'amount': (1 << 63)})
db = expect_error('amount beyond int64 rejected', 'OUT_OF_SCOPE',
                  lambda: put_view(db, rows, token), db, HIDDEN)
rows[-1]['amount'] = -(1 << 63)
put_view(db, rows, token)
check('int64-min amount accepted and stored',
      db.execute('SELECT amount FROM orders WHERE order_id=?', ('big',)).fetchone() == (-(1 << 63),))

# A failure occurring after earlier writes in the same request must roll
# everything back. Force a constraint failure mid-write (duplicate primary key
# created by a trigger, simulating any database-level constraint error).
db.execute('''CREATE TRIGGER fail_later AFTER INSERT ON orders
              WHEN NEW.order_id = 'boom'
              BEGIN INSERT INTO orders(order_id, customer_id, amount, status)
                    VALUES ('open-a', 'c1', 1, 'open'); END''')
db.commit()
rows, token = open_view(db)
rows[0]['amount'] += 1
rows.append({'order_id': 'boom', 'customer_id': 'c1', 'customer_name': 'Alice', 'amount': 7})
before = dump(db)
try:
    put_view(db, rows, token)
    check('database constraint failure aborts the whole request', False)
except sqlite3.IntegrityError:
    check('database constraint failure aborts the whole request', True)
check('rollback undid earlier writes of the same request too', dump(db) == before)
check('connection remains usable after rollback', read_view(db) == open_view(db)[0])
db.close()

section('scoped view: nested transaction and new-row defaults')
db, p = fresh_db(sseed); _current_path = p
rows, token = open_view(db, scope)
rows.append({'order_id': 'brand-new', 'customer_id': 'c9', 'customer_name': 'Nine', 'amount': 99})
db.execute('BEGIN')
try:
    put_view(db, rows, token)  # uses SAVEPOINT internally
    db.commit()
except Exception:
    db.rollback()
    raise
check('new order gets schema default internal_note and open status',
      db.execute("SELECT customer_id,amount,status,internal_note FROM orders WHERE order_id='brand-new'").fetchone()
      == ('c9', 99, 'open', ''))
check('new customer gets schema default private_note',
      db.execute("SELECT name,private_note FROM customers WHERE customer_id='c9'").fetchone()
      == ('Nine', ''))
# Failing nested call must not poison the outer transaction.
rows2, token2 = open_view(db, scope)
rows2.append({'order_id': 'closed', 'customer_id': 'c2', 'customer_name': 'other', 'amount': 10})
db.execute('BEGIN')
try:
    put_view(db, rows2, token2)
    check('nested failure rolls back to savepoint', False)
except ViewEditError as e:
    check('nested failure rolls back to savepoint [' + e.code + ']', e.code == 'CONFLICT')
db.commit()
check('outer transaction still alive and its writes persisted',
      db.execute("SELECT 1 FROM orders WHERE order_id='brand-new'").fetchone() is not None)
db.close()

print('\n%d passed, %d failed' % (len(PASSED), len(FAILED)))
if FAILED:
    raise SystemExit(1)
print('all scenarios verified on real SQLite')
