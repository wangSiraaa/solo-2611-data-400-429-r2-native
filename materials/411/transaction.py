from validation import conflict


class Transaction:
    def __init__(self, store):
        self.store = store
        self.start = store.revision
        self.writes = {}
        self.state = 'ACTIVE'

    def scan(self, low, high, on_call=None):
        rows = self.store.snapshot(self.start)
        for key, value in self.writes.items():
            if value is None:
                rows.pop(key, None)
            else:
                rows[key] = value
        return [rows[key].copy() for key in sorted(rows)
                if low <= rows[key]['team'] <= high
                and (on_call is None or rows[key]['on_call'] == on_call)]

    def put(self, row):
        self.writes[row['row_id']] = row.copy()

    def commit(self):
        if self.state != 'ACTIVE':
            return self.state
        if conflict(self.store, self):
            self.state = 'CONFLICT'
        else:
            self.store.publish(self.writes)
            self.state = 'COMMITTED'
        return self.state
