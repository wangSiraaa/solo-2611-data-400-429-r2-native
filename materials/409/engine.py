"""In-place delta engine with STEAL/NO-FORCE pages and WAL-before-page writes.

Rules enforced here:

* Business ADD deltas and persisted row values stay inside signed int64;
  booleans are rejected (``bool`` is not treated as an integer).  An overflowing
  ADD is rejected as a whole: no log record, no page change, no lock taken.
* At most one open writer per (page, key); a second writer gets LOCKED instead
  of waiting.  Locks release when the transaction reaches a durable outcome.
* flush may evict pages modified by still-active transactions (STEAL) and
  commit never requires dirty pages to reach disk (NO-FORCE), but every page
  write is preceded by a real, reopenable force of the WAL prefix through the
  page's page_lsn.  The unsafe-flush state (page ahead of durable WAL) is
  therefore impossible to produce.
* Construction runs restart recovery first; business entries are only usable
  once every transaction left over from an earlier process is resolved.
"""
from page_io import Pages
from recovery import INT64_MAX, INT64_MIN, RecoveryError, recover
from wal import Journal


class LockedError(ValueError):
    """Another open transaction already owns the row."""


class Engine:
    def __init__(self, root, run_recovery=True):
        self.root = str(root)
        self.pages = Pages(self.root + '/pages')
        self.wal = Journal(root)
        self.buffer = {}
        self.transactions = {}
        self.locks = {}
        self.audit = []
        self.recovery_report = None
        if run_recovery:
            self.recovery_report = recover(root)
            if self.recovery_report['status'] != 'ready':
                raise RecoveryError('startup recovery did not reach READY')

    def _record(self, tx, kind, **fields):
        state = self.transactions[tx]
        lsn = self.wal.append({'tx': tx, 'kind': kind, 'prev_lsn': state['last'], **fields})
        state['last'] = lsn
        return lsn

    def begin(self, tx):
        if tx in self.transactions:
            raise ValueError('duplicate transaction in this process')
        # Transaction ids are unique for the working directory, including
        # across the processes separated by a crash.
        if any(record['tx'] == tx for record in self.wal.records()):
            raise ValueError('transaction id %r already used in this directory' % tx)
        self.transactions[tx] = {'last': 0, 'state': 'active'}
        self._record(tx, 'BEGIN')

    def _page(self, page_id):
        if page_id not in self.buffer:
            self.buffer[page_id] = self.pages.read(page_id)
        return self.buffer[page_id]

    def add(self, tx, page_id, key, delta):
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise ValueError('ADD delta must be a signed int64 integer, not bool/float')
        if not INT64_MIN <= delta <= INT64_MAX:
            raise ValueError('ADD delta is outside signed int64')
        state = self.transactions[tx]
        if state['state'] != 'active':
            raise ValueError('transaction is terminal')
        page = self._page(page_id)
        if key not in page['rows']:
            raise ValueError('unknown row %s on page %s; keys are fixed' % (key, page_id))
        current = page['rows'][key]
        new_value = current + delta
        if not INT64_MIN <= new_value <= INT64_MAX:
            # Whole-request rejection: nothing is appended, locked or changed.
            raise OverflowError('ADD would push %s/%s outside signed int64' % (page_id, key))
        owner = self.locks.get((page_id, key))
        if owner is not None and owner != tx:
            raise LockedError('LOCKED: row %s/%s is owned by transaction %r'
                              % (page_id, key, owner))
        self.locks[(page_id, key)] = tx
        lsn = self._record(tx, 'ADD', page=page_id, key=key, delta=delta)
        page['rows'][key] = new_value
        page['page_lsn'] = lsn
        self.audit.append({'operation': 'add', 'tx': tx, 'page': page_id, 'key': key,
                           'delta': delta, 'lsn': lsn, 'new_value': new_value})
        return lsn

    def _release_locks(self, tx):
        for locked_key in [locked_key for locked_key, owner in self.locks.items()
                           if owner == tx]:
            del self.locks[locked_key]

    def _finish(self, tx, kind):
        lsn = self._record(tx, kind)
        # Durable decision: the log is force/fsynced, dirty pages need not be.
        self.wal.force(lsn)
        self.transactions[tx]['state'] = kind
        self._release_locks(tx)
        self.audit.append({'operation': kind.lower(), 'tx': tx, 'lsn': lsn})
        return lsn

    def commit(self, tx):
        if tx not in self.transactions or self.transactions[tx]['state'] != 'active':
            raise ValueError('cannot commit an unknown or terminal transaction')
        return self._finish(tx, 'COMMIT')

    def abort(self, tx):
        if tx not in self.transactions or self.transactions[tx]['state'] != 'active':
            raise ValueError('cannot abort an unknown or terminal transaction')
        state = self.transactions[tx]
        # Reverse every ADD not already compensated (a transaction can only be
        # aborted once here, but keep the check so behavior matches recovery).
        compensated = {record['undo_of'] for record in self.wal.records()
                       if record['kind'] == 'UNDO'}
        for record in reversed(
                [entry for entry in self.wal.records()
                 if entry['tx'] == tx and entry['kind'] == 'ADD']):
            if record['lsn'] in compensated:
                continue
            lsn = self._record(tx, 'UNDO', page=record['page'], key=record['key'],
                               delta=-record['delta'], undo_of=record['lsn'],
                               undo_next=record['prev_lsn'])
            page = self._page(record['page'])
            page['rows'][record['key']] -= record['delta']
            page['page_lsn'] = lsn
        return self._finish(tx, 'ABORT')

    def flush(self, page_id):
        page = self._page(page_id)
        required_lsn = page['page_lsn']
        if required_lsn:
            # WAL-before-page: force every physical record this page contains,
            # then re-open the real file instead of trusting a cached claim.
            self.wal.force(required_lsn)
            reopened = self.wal.durable_lsn()
            if reopened < required_lsn:
                raise RuntimeError(
                    'WAL prefix ends at lsn=%s but page %s needs lsn=%s'
                    % (reopened, page_id, required_lsn))
        self.audit.append({'page': page_id, 'page_lsn': required_lsn,
                           'durable_lsn_before_page_write': self.wal.durable_lsn()})
        self.pages.write(page_id, page)
