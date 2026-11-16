import json
import sqlite3
from pathlib import Path
from view import open_view
from writeback import put_view

root = Path(__file__).parent
case = json.loads((root / 'scoped-cases.json').read_text())
db = sqlite3.connect(':memory:')
db.executescript((root / 'schema.sql').read_text())
db.executemany('INSERT INTO customers VALUES (?,?,?)', case['customers'])
db.executemany('INSERT INTO orders VALUES (?,?,?,?,?)', case['orders'])
db.commit()

# The scoped read now returns a credential binding the normalized scope and
# the visible member's original public values.
desired, token = open_view(db, case['scope'])
assert [row['order_id'] for row in desired] == ['inside']
result = put_view(db, desired, token)

actual = [row[0] for row in db.execute('SELECT order_id FROM orders ORDER BY order_id')]
assert actual == case['empty_edit_expected_order_ids'], actual
hidden_notes = dict(db.execute('SELECT order_id, internal_note FROM orders ORDER BY order_id'))
assert hidden_notes == {'inside': 'in-note', 'outside': 'out-note', 'closed': 'closed-note'}
secrets = dict(db.execute('SELECT customer_id, private_note FROM customers ORDER BY customer_id'))
assert secrets == {'c1': 'secret-one', 'c2': 'secret-two'}
# A re-read through the same bound scope must reproduce the target exactly.
reread, _ = open_view(db, case['scope'])
assert reread == desired

print(json.dumps({'baseline_after_scoped_empty_edit': actual,
                  'required_order_ids': case['empty_edit_expected_order_ids'],
                  'put_result': result,
                  'hidden_notes': hidden_notes,
                  'private_notes_kept': secrets,
                  'scope': 'Credentialed scoped read + relational diff on real SQLite; '
                           'out-of-scope and closed rows, internal_note and private_note are retained.'},
                 ensure_ascii=False))
