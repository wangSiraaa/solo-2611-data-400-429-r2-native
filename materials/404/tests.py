"""Exercise the incremental session and its SQLite publication boundary."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import batch
from session import Session
from sql_consumer import snapshot


def write_fixture(directory, name, frames):
    path = Path(directory) / (name + '.json')
    path.write_text(json.dumps(frames))
    return path


def order(oid, **fields):
    value = {'id': oid}
    value.update(fields)
    return value


def line(lid, qty):
    return {'id': lid, 'qty': qty}


def patch(path, **entry):
    return {'path': path, **entry}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.db = self.dir / 'preview.sqlite'

    def tearDown(self):
        self._dir.cleanup()

    def session(self, frames, name='case'):
        return Session(write_fixture(self.dir, name, frames), self.db)

    def assertPreview(self, preview, status, last_frame, orders, lines):
        self.assertEqual(preview['status'], status)
        self.assertEqual(preview['last_frame'], last_frame)
        self.assertEqual(
            [{k: r[k] for k in ('id', 'tax', 'source_path')} for r in preview['orders']],
            orders)
        self.assertEqual(
            [{k: r[k] for k in ('order_id', 'id', 'qty', 'source_path')}
             for r in preview['lines']],
            lines)

    def test_single_frame_completes_immediately(self):
        session = self.session([{
            'data': {'orders': [order('o1', tax=10, lines=[line('l1', 2)])]},
            'hasNext': False,
        }])
        preview = session.pump()
        self.assertPreview(preview, 'complete', 1,
                           [{'id': 'o1', 'tax': 10, 'source_path': '["orders",0]'}],
                           [{'order_id': 'o1', 'id': 'l1', 'qty': 2,
                             'source_path': '["orders",0,"lines",0]'}])
        # Repeated queries and repeated legal pumps add no rows.
        self.assertEqual(session.query(), preview)
        self.assertEqual(session.pump(), preview)
        self.assertEqual(snapshot(self.db), preview)

    def test_deferred_tax_patch_merges_and_preserves_provenance(self):
        session = self.session([
            {'data': {'orders': [order('o1', lines=[line('l1', 2)])]},
             'hasNext': True},
            {'incremental': [patch(['orders', 0], data={'tax': 10})],
             'hasNext': False},
        ])
        first = session.pump()
        self.assertPreview(first, 'receiving', 1,
                           [{'id': 'o1', 'tax': None,
                             'source_path': '["orders",0]'}],
                           [{'order_id': 'o1', 'id': 'l1', 'qty': 2,
                             'source_path': '["orders",0,"lines",0]'}])
        second = session.pump()
        self.assertPreview(second, 'complete', 2,
                           [{'id': 'o1', 'tax': 10,
                             'source_path': '["orders",0]'}],
                           [{'order_id': 'o1', 'id': 'l1', 'qty': 2,
                             'source_path': '["orders",0,"lines",0]'}])
        # Exactly one order and one line: a merge did not duplicate them.
        self.assertEqual(len(second['orders']), 1)
        self.assertEqual(second['orders'][0]['created_frame'], 1)
        self.assertEqual(second['orders'][0]['last_changed_frame'], 2)
        self.assertEqual(second['lines'][0]['last_changed_frame'], 1)

    def test_deferred_fixture_visible_after_first_frame(self):
        path = Path(__file__).with_name('fixtures') / 'deferred.json'
        session = Session(path, self.db)
        first = session.pump()
        self.assertEqual(first['status'], 'receiving')
        self.assertEqual(first['orders'][0]['id'], 'o1')
        self.assertEqual(first['lines'][0]['id'], 'l1')
        self.assertEqual(first['orders'][0]['tax'], None)

    def test_null_bubble_removes_ancestor_and_all_lines(self):
        path = Path(__file__).with_name('fixtures') / 'null-bubble.json'
        session = Session(path, self.db)
        session.pump()
        inserted = session.pump()
        self.assertEqual([r['id'] for r in inserted['lines']], ['l1', 'l2'])
        self.assertEqual(inserted['lines'][1]['created_frame'], 2)
        final = session.pump()
        self.assertEqual(final['status'], 'complete_with_errors')
        self.assertEqual(final['orders'], [])
        self.assertEqual(final['lines'], [])
        self.assertEqual(final['errors'][0]['path'], ['orders', 0, 'tax'])
        self.assertEqual(final['last_frame'], 3)

    def test_items_insertion_shifts_paths_and_marks_changed(self):
        session = self.session([
            {'data': {'orders': [order('o1', lines=[line('l1', 2)])]},
             'hasNext': True},
            {'incremental': [patch(['orders', 0, 'lines', 0],
                                   items=[line('l0', 7)])],
             'hasNext': True},
            {'incremental': [], 'hasNext': False},
        ])
        session.pump()
        shifted = session.pump()
        lines = {r['id']: r for r in shifted['lines']}
        self.assertEqual(lines['l0']['source_path'], '["orders",0,"lines",0]')
        self.assertEqual(lines['l0']['created_frame'], 2)
        self.assertEqual(lines['l1']['source_path'], '["orders",0,"lines",1]')
        # Path changed even though the line's value did not.
        self.assertEqual(lines['l1']['created_frame'], 1)
        self.assertEqual(lines['l1']['last_changed_frame'], 2)
        done = session.pump()
        self.assertEqual(done['status'], 'complete')
        self.assertEqual(done['last_frame'], 3)
        # An unchanged frame does not move last_changed_frame.
        self.assertEqual({r['id']: r['last_changed_frame']
                          for r in done['lines']}, {'l0': 2, 'l1': 2})

    def test_order_insertion_shifts_order_paths(self):
        session = self.session([
            {'data': {'orders': [order('o1')]}, 'hasNext': True},
            {'incremental': [patch(['orders', 0], items=[order('o0')])],
             'hasNext': False},
        ])
        session.pump()
        preview = session.pump()
        orders = {r['id']: r for r in preview['orders']}
        self.assertEqual(orders['o0']['source_path'], '["orders",0]')
        self.assertEqual(orders['o1']['source_path'], '["orders",1]')
        self.assertEqual(orders['o1']['last_changed_frame'], 2)

    def test_null_list_element_keeps_neighbour_positions(self):
        session = self.session([
            {'data': {'orders': [order('o1', lines=[line('l1', 1),
                                                    line('l2', 2)])]},
             'hasNext': True},
            {'incremental': [patch(['orders', 0, 'lines', 0], data=None)],
             'hasNext': False},
        ])
        session.pump()
        preview = session.pump()
        self.assertEqual([r['id'] for r in preview['lines']], ['l2'])
        self.assertEqual(preview['lines'][0]['source_path'],
                         '["orders",0,"lines",1]')
        self.assertEqual(preview['status'], 'complete')

    def test_zero_and_explicit_null_are_preserved(self):
        session = self.session([{
            'data': {'orders': [order('o1', tax=None,
                                      lines=[line('l1', 0)])]},
            'hasNext': False,
        }])
        preview = session.pump()
        self.assertIsNone(preview['orders'][0]['tax'])
        self.assertEqual(preview['lines'][0]['qty'], 0)

    def test_truncated_eof_is_incomplete_and_keeps_preview(self):
        path = Path(__file__).with_name('fixtures') / 'truncated.json'
        session = Session(path, self.db)
        first = session.pump()
        self.assertEqual(first['status'], 'receiving')
        eof = session.pump()
        self.assertEqual(eof['status'], 'incomplete')
        self.assertEqual(eof['last_frame'], 1)
        self.assertEqual(eof['orders'], first['orders'])
        self.assertEqual(eof['lines'], first['lines'])
        # The preview is queryable but never marked successful.
        self.assertEqual(session.query(), eof)

    def test_duplicate_identity_fails_without_publishing_or_reading_on(self):
        path = Path(__file__).with_name('fixtures') / 'duplicate-id.json'
        session = Session(path, self.db)
        first = session.pump()
        calls = session.source.calls
        failed = session.pump()
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['last_frame'], 1)
        self.assertEqual(failed['orders'], first['orders'])
        self.assertEqual(failed['lines'], first['lines'])
        self.assertIn('duplicate', failed['errors'][-1]['message'])
        # The failing enveloped was consumed; nothing after it is.
        calls = session.source.calls
        session.pump()
        self.assertEqual(session.source.calls, calls)

    def test_duplicate_order_id_is_a_failure(self):
        session = self.session([{
            'data': {'orders': [order('o1'), order('o1')]},
            'hasNext': False,
        }])
        failed = session.pump()
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['last_frame'], 0)
        self.assertEqual(failed['orders'], [])

    def test_protocol_error_fails_and_preserves_last_frame(self):
        session = self.session([
            {'data': {'orders': [order('o1')]}, 'hasNext': True},
            {'incremental': [patch(['nope', 'x'], data={'x': 1})],
             'hasNext': False},
        ])
        good = session.pump()
        failed = session.pump()
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['last_frame'], 1)
        self.assertEqual(failed['orders'], good['orders'])
        self.assertEqual(failed['errors'][-1]['type'], 'protocol_error')

    def test_non_boolean_has_next_is_protocol_error(self):
        session = self.session([{'data': {}, 'hasNext': 'false'}])
        self.assertEqual(session.pump()['status'], 'failed')

    def test_data_in_later_envelope_is_protocol_error(self):
        session = self.session([
            {'data': {}, 'hasNext': True},
            {'data': {}, 'hasNext': False},
        ])
        session.pump()
        self.assertEqual(session.pump()['status'], 'failed')

    def test_frame_after_terminal_is_protocol_error(self):
        session = self.session([
            {'data': {'orders': []}, 'hasNext': False},
            {'incremental': [], 'hasNext': True},
        ])
        complete = session.pump()
        self.assertEqual(complete['status'], 'complete')
        after = session.pump()
        self.assertEqual(after['status'], 'failed')
        self.assertEqual(after['last_frame'], 1)

    def test_more_than_sixteen_envelopes_fails(self):
        frames = [{'data': {'orders': []}, 'hasNext': True}] * 16
        frames.append({'incremental': [], 'hasNext': False})
        session = self.session(frames)
        for _ in range(16):
            session.pump()
        self.assertEqual(session.pump()['status'], 'failed')

    def test_publish_hook_pauses_before_publication(self):
        session = self.session([
            {'data': {'orders': [order('o1', tax=10,
                                      lines=[line('l1', 2)])]},
             'hasNext': True},
            {'incremental': [patch(['orders', 0], data={'tax': 20})],
             'hasNext': False},
        ])
        session.pump()
        entered = threading.Event()
        release = threading.Event()
        observed = {}

        def hook():
            entered.set()
            release.wait(5)

        session.publish_hook = hook
        worker = threading.Thread(target=session.pump)
        worker.start()
        self.assertTrue(entered.wait(5))
        # While the writer is paused, an independent reader still sees frame 1.
        observed['during'] = snapshot(self.db)
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())

        self.assertEqual(observed['during']['last_frame'], 1)
        self.assertEqual(observed['during']['status'], 'receiving')
        self.assertEqual(observed['during']['orders'][0]['tax'], 10)
        after = snapshot(self.db)
        self.assertEqual(after['last_frame'], 2)
        self.assertEqual(after['status'], 'complete')
        self.assertEqual(after['orders'][0]['tax'], 20)
        self.assertEqual(session.query(), after)

    def test_injected_publish_failure_preserves_last_frame(self):
        session = self.session([
            {'data': {'orders': [order('o1', tax=10,
                                      lines=[line('l1', 2)])]},
             'hasNext': True},
            {'incremental': [patch(['orders', 0], data={'tax': 20})],
             'hasNext': True},
            {'incremental': [patch(['orders', 0], data={'tax': 30})],
             'hasNext': False},
        ])
        session.pump()

        def fail():
            raise OSError('injected publication failure')

        session.publish_hook = fail
        failed = session.pump()
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['last_frame'], 1)
        self.assertEqual(failed['orders'][0]['tax'], 10)
        self.assertEqual(failed['lines'][0]['qty'], 2)
        self.assertEqual(failed['orders'][0]['last_changed_frame'], 1)
        self.assertEqual(failed['errors'][-1]['type'], 'publish_error')
        self.assertEqual(snapshot(self.db), failed)
        # The failing frame was consumed; no further frame is.
        calls = session.source.calls
        session.pump()
        self.assertEqual(session.source.calls, calls)

    def test_preview_database_is_an_independent_product(self):
        session = self.session([{
            'data': {'orders': [order('o1', tax=10,
                                      lines=[line('l1', 2)])]},
            'hasNext': False,
        }])
        preview = session.pump()
        independent = snapshot(self.db)
        self.assertEqual(independent, preview)
        for row in independent['orders'] + independent['lines']:
            self.assertIsInstance(row['source_path'], str)
            json.loads(row['source_path'])

    def test_open_read_transaction_sees_one_whole_frame(self):
        session = self.session([
            {'data': {'orders': [order('o1', tax=10,
                                      lines=[line('l1', 2)])]},
             'hasNext': True},
            {'incremental': [patch(['orders', 0], data={'tax': 20})],
             'hasNext': False},
        ])
        session.pump()

        reader = sqlite3.connect(self.db)
        reader.row_factory = sqlite3.Row
        reader.execute('BEGIN')
        pinned_meta = dict(reader.execute(
            'SELECT status,last_frame FROM preview_meta').fetchone())
        pinned_orders = [dict(r) for r in reader.execute(
            'SELECT * FROM orders ORDER BY id')]
        pinned_lines = [dict(r) for r in reader.execute(
            'SELECT * FROM lines ORDER BY order_id,id')]

        session.pump()

        # The already-open transaction still reads the old committed frame.
        self.assertEqual(pinned_meta, {'status': 'receiving', 'last_frame': 1})
        self.assertEqual(pinned_orders[0]['tax'], 10)
        self.assertEqual(pinned_lines[0]['qty'], 2)
        reader.execute('COMMIT')
        reader.close()

        fresh = snapshot(self.db)
        self.assertEqual(fresh['last_frame'], 2)
        self.assertEqual(fresh['orders'][0]['tax'], 20)

    def test_one_argument_session_uses_a_temporary_database(self):
        path = Path(__file__).with_name('fixtures') / 'single.json'
        session = Session(path)
        preview = session.pump()
        self.assertEqual(preview['status'], 'complete')
        self.assertTrue(Path(session.db_path).exists())
        session._temp.cleanup()

    def test_batch_entry_point_accepts_database_path(self):
        path = Path(__file__).with_name('fixtures') / 'null-bubble.json'
        result = batch.run(path, self.db)
        self.assertEqual(result['db_path'], str(self.db))
        self.assertEqual([s['status'] for s in result['snapshots']],
                         ['receiving', 'receiving', 'complete_with_errors'])
        final = result['snapshots'][-1]
        self.assertEqual(snapshot(self.db), final)
        self.assertEqual(final['orders'], [])

    def test_absent_object_field_may_be_added(self):
        session = self.session([
            {'data': {'orders': [order('o1')]}, 'hasNext': True},
            {'incremental': [patch(['orders', 0, 'meta'], data={'note': 'x'})],
             'hasNext': False},
        ])
        session.pump()
        # The extra field is not projected, but the patch is legal.
        self.assertEqual(session.pump()['status'], 'complete')

    def test_out_of_range_items_insertion_is_protocol_error(self):
        session = self.session([
            {'data': {'orders': [order('o1', lines=[line('l1', 1)])]},
             'hasNext': True},
            {'incremental': [patch(['orders', 0, 'lines', 5],
                                   items=[line('l2', 2)])],
             'hasNext': False},
        ])
        session.pump()
        self.assertEqual(session.pump()['status'], 'failed')

    def test_bounds_are_enforced(self):
        too_many_orders = {'data': {'orders': [order('o%d' % i)
                                               for i in range(21)]},
                           'hasNext': False}
        session = self.session([too_many_orders])
        self.assertEqual(session.pump()['status'], 'failed')

        too_many_lines = {
            'data': {'orders': [order('o1', lines=[
                line('l%d' % i, i) for i in range(51)])]},
            'hasNext': False}
        session = self.session([too_many_lines], name='bounds2')
        self.assertEqual(session.pump()['status'], 'failed')

        deep = {'data': {'orders': [order('o1')]}, 'hasNext': True}
        deep_patch = {'incremental': [patch(['a', 'b', 'c', 'd', 'e', 'f'],
                                            data={})], 'hasNext': False}
        session = self.session([deep, deep_patch], name='bounds3')
        session.pump()
        self.assertEqual(session.pump()['status'], 'failed')

    def test_null_order_removes_lines_and_shifts_survivors(self):
        session = self.session([
            {'data': {'orders': [
                order('o1', tax=1, lines=[line('a1', 1)]),
                order('o2', tax=2, lines=[line('b1', 2), line('b2', 3)]),
            ]}, 'hasNext': True},
            {'incremental': [patch(['orders', 0], data=None)],
             'hasNext': False},
        ])
        before = session.pump()
        self.assertEqual(len(before['lines']), 3)
        after = session.pump()
        self.assertEqual([r['id'] for r in after['orders']], ['o2'])
        self.assertEqual([r['id'] for r in after['lines']], ['b1', 'b2'])
        # The nulled list element keeps its position, so o2 stays at index 1
        # and its own source paths do not change.
        self.assertEqual(after['orders'][0]['source_path'], '["orders",1]')
        self.assertEqual(after['lines'][0]['source_path'],
                         '["orders",1,"lines",0]')
        self.assertEqual(after['lines'][1]['source_path'],
                         '["orders",1,"lines",1]')
        self.assertEqual(after['orders'][0]['created_frame'], 1)
        self.assertEqual(after['orders'][0]['last_changed_frame'], 1)
        self.assertEqual({r['id']: (r['created_frame'], r['last_changed_frame'])
                          for r in after['lines']},
                         {'b1': (1, 1), 'b2': (1, 1)})
        # The removed order and its line are gone from SQL too.
        self.assertEqual(snapshot(self.db), after)

    def test_ancestor_invalidation_cost_is_reported(self):
        session = self.session([
            {'data': {'orders': [order('o1', lines=[line('l1', 1),
                                                    line('l2', 2)]),
                                 order('o2')]},
             'hasNext': True},
            {'incremental': [patch(['orders', 0], data=None)],
             'hasNext': False},
        ])
        session.pump()
        before = session.last_stats['nodes']
        session.pump()
        # The invalidation walk drops the removed order's lines and keeps the
        # surviving (now re-indexed) order in the list.
        self.assertEqual(before, 2 + 2)
        self.assertEqual(session.last_stats['nodes'], 2)
        preview = session.query()
        self.assertEqual([r['id'] for r in preview['orders']], ['o2'])
        self.assertEqual(preview['orders'][0]['source_path'], '["orders",1]')
        self.assertEqual(preview['lines'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
