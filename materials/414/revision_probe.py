import json
import sqlite3
from pathlib import Path
from scope_reader import read_scope
from writeback import put_view

root = Path(__file__).parent
case = json.loads((root / 'scoped-cases.json').read_text())
db = sqlite3.connect(':memory:')
db.executescript((root / 'schema.sql').read_text())
db.executemany('INSERT INTO customers VALUES (?,?,?)', case['customers'])
db.executemany('INSERT INTO orders VALUES (?,?,?,?,?)', case['orders'])
db.commit()
desired = read_scope(db, case['scope'])
assert [row['order_id'] for row in desired] == ['inside']
put_view(db, desired)
actual = [row[0] for row in db.execute('SELECT order_id FROM orders ORDER BY order_id')]
assert actual == ['inside']
print(json.dumps({'baseline_after_scoped_empty_edit': actual,
                  'required_order_ids': case['empty_edit_expected_order_ids'],
                  'scope': 'Real SQLite and existing scoped reader/writeback; target credential and diff are absent.'}))
