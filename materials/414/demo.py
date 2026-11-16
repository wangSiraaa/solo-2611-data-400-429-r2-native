import json
from pathlib import Path
import sqlite3
from view import read_view
from writeback import put_view

def seed(hidden):
    db=sqlite3.connect(':memory:')
    db.executescript(Path('schema.sql').read_text())
    db.execute('INSERT INTO customers VALUES(?,?,?)',('c1','Alice','protected' if hidden else ''))
    db.execute('INSERT INTO orders VALUES(?,?,?,?,?)',('open','c1',10,'open','kept' if hidden else ''))
    if hidden: db.execute('INSERT INTO orders VALUES(?,?,?,?,?)',('closed','c1',20,'closed','archive'))
    db.commit()
    return db
plain=seed(False)
desired=read_view(plain)
put_view(plain,desired)
normal=read_view(plain)==desired
db=seed(True)
desired=read_view(db)
put_view(db,desired)
actual=dict(orders=db.execute('SELECT order_id,internal_note FROM orders ORDER BY order_id').fetchall(),
            customer_notes=db.execute('SELECT private_note FROM customers').fetchall())
print(json.dumps(dict(normal_path_ok=normal,
    normal_observation='没有隐藏信息的完整开放订单视图可以往返。',
    counterexample_detected=len(actual['orders'])!=2 or actual['customer_notes']!=[('protected',)],
    actual=actual,
    failure_observation='未作任何前端编辑，写回仍删掉closed订单，并将private_note与internal_note恢复默认空串。',
    oracle_basis='空编辑必须满足GetPut：所有基础行及全部隐藏字段保持原值，read_view仍等于原视图。'),ensure_ascii=False))
