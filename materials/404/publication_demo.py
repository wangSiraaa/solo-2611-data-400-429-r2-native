"""Verify whole-frame SQLite publication with independent SQLite connections.

Each case reads through a connection separate from the publisher.  The two
former defects are now checked directly:

* during publication an outside reader still sees the previous frame;
* an injected ``OSError`` rolls the whole frame back, not just metadata.
"""

import json
import tempfile
from pathlib import Path

from sqlite_view import SQLiteView
from sql_consumer import snapshot


def frame(n, tax, status='receiving'):
    return {'status': status, 'last_frame': n, 'errors': [],
            'orders': [{'id': 'o1', 'tax': tax,
                        'path': ('orders', 0),
                        'created_frame': 1, 'last_changed_frame': n}],
            'lines': [{'order_id': 'o1', 'id': 'l1', 'qty': n,
                       'path': ('orders', 0, 'lines', 0),
                       'created_frame': 1, 'last_changed_frame': n}]}


def rows(preview):
    return [{k: r[k] for k in ('id', 'tax', 'source_path',
                               'created_frame', 'last_changed_frame')}
            for r in preview['orders']], [
                {k: r[k] for k in ('order_id', 'id', 'qty', 'source_path',
                                   'created_frame', 'last_changed_frame')}
                for r in preview['lines']]


with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'view.sqlite'
    view = SQLiteView(path)
    view.publish(frame(1, 10))
    normal = snapshot(path)
    print(json.dumps({'case': 'normal', 'actual': normal,
                      'expected_frame': 1, 'expected_tax': 10,
                      'ok': normal['last_frame'] == 1
                      and normal['orders'][0]['tax'] == 10}))

    seen = []
    view.publish(frame(2, 20), lambda: seen.append(snapshot(path)))
    during = seen[0]
    print(json.dumps({'case': 'during_publish', 'actual': during,
                      'expected_frame': 1, 'expected_tax': 10,
                      'expected_qty': 1,
                      'ok': during['last_frame'] == 1
                      and during['orders'][0]['tax'] == 10
                      and during['lines'][0]['qty'] == 1}))

    published = snapshot(path)
    print(json.dumps({'case': 'after_publish', 'actual': published,
                      'expected_frame': 2, 'expected_tax': 20,
                      'ok': published['last_frame'] == 2
                      and published['orders'][0]['tax'] == 20}))

    before = snapshot(path)

    def fail():
        raise OSError('injected publication failure')

    try:
        view.publish(frame(3, 30), fail)
    except OSError:
        pass
    after = snapshot(path)
    print(json.dumps({'case': 'failed_publish', 'before': before,
                      'actual': after, 'expected_rows_equal_before': True,
                      'ok': rows(before) == rows(after)
                      and before['last_frame'] == after['last_frame']
                      and before['status'] == after['status']}))

    # The session turns that same rollback into a terminal failed state.
    from session import Session
    session = Session(Path(__file__).with_name('fixtures') / 'deferred.json',
                      Path(directory) / 'session.sqlite')
    session.pump()
    session.publish_hook = fail
    failed = session.pump()
    print(json.dumps({'case': 'session_failed', 'actual': failed,
                      'expected_status': 'failed', 'expected_frame': 1,
                      'ok': failed['status'] == 'failed'
                      and failed['last_frame'] == 1
                      and failed['orders'][0]['tax'] is None}))
