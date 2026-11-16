"""Observe split publication and rollback defects without solving them."""
import json
import tempfile
from pathlib import Path
from sqlite_view import SQLiteView
from sql_consumer import snapshot


def frame(n, tax, status='receiving'):
    return {'status':status, 'last_frame':n, 'errors':[],
            'orders':[{'id':'o1','tax':tax,'source_path':'["orders",0]',
                       'created_frame':1,'last_changed_frame':n}],
            'lines':[{'order_id':'o1','id':'l1','qty':n,'source_path':'["orders",0,"lines",0]',
                      'created_frame':1,'last_changed_frame':n}]}


with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/'view.sqlite';view=SQLiteView(path)
    view.publish(frame(1,10));normal=snapshot(path)
    seen=[]
    view.publish(frame(2,20),lambda:seen.append(snapshot(path)))
    print(json.dumps({'case':'normal','actual':normal,'expected_frame':1,'expected_tax':10}))
    print(json.dumps({'case':'during_publish','actual':seen[0],
                      'expected_frame':1,'expected_tax':10,'expected_qty':1}))
    before=snapshot(path)
    def fail():raise OSError('injected publication failure')
    try:view.publish(frame(3,30),fail)
    except OSError:pass
    print(json.dumps({'case':'failed_publish','before':before,'actual':snapshot(path),
                      'expected_rows_equal_before':True}))
