"""Normal updates work; pre-commit page eviction and abort durability are unsafe."""
from page_io import Pages
from wal import Journal


class Engine:
    def __init__(self, root):
        self.pages = Pages(str(root) + '/pages')
        self.wal = Journal(root)
        self.buffer = {}
        self.transactions = {}
        self.locks = {}
        self.audit = []

    def _record(self, tx, kind, **fields):
        state = self.transactions[tx]
        lsn = self.wal.append({'tx': tx, 'kind': kind, 'prev_lsn': state['last'], **fields})
        state['last'] = lsn
        return lsn

    def begin(self, tx):
        if tx in self.transactions:
            raise ValueError('duplicate transaction')
        self.transactions[tx] = {'last': 0, 'state': 'active'}
        self._record(tx, 'BEGIN')

    def _page(self, page_id):
        if page_id not in self.buffer:
            self.buffer[page_id] = self.pages.read(page_id)
        return self.buffer[page_id]

    def add(self, tx, page_id, key, delta):
        if self.transactions[tx]['state'] != 'active':
            raise ValueError('transaction is terminal')
        owner = self.locks.get((page_id, key))
        if owner is not None and owner != tx:
            raise ValueError('row is write-locked')
        self.locks[(page_id, key)] = tx
        page = self._page(page_id)
        lsn = self._record(tx, 'ADD', page=page_id, key=key, delta=delta)
        page['rows'][key] += delta
        page['page_lsn'] = lsn

    def _finish(self, tx, state):
        lsn = self._record(tx, state)
        self.wal.force(lsn)
        self.transactions[tx]['state'] = state
        for key in [key for key, owner in self.locks.items() if owner == tx]:
            del self.locks[key]

    def commit(self, tx):
        self._finish(tx, 'COMMIT')

    def abort(self, tx):
        for record in reversed(self.wal.records()):
            if record['tx'] == tx and record['kind'] == 'ADD':
                lsn = self._record(tx, 'UNDO', page=record['page'], key=record['key'],
                                   delta=-record['delta'], undo_of=record['lsn'],
                                   undo_next=record['prev_lsn'])
                page = self._page(record['page'])
                page['rows'][record['key']] -= record['delta']
                page['page_lsn'] = lsn
        self._finish(tx, 'ABORT')

    def flush(self, page_id):
        page = self._page(page_id)
        # The ordinary buffer writer knows dirty pages but not WAL durability.
        self.audit.append({'page': page_id, 'page_lsn': page['page_lsn'],
                           'durable_lsn_before_page_write': self.wal.durable_lsn()})
        self.pages.write(page_id, page)
