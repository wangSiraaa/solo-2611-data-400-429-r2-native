class Store:
    def __init__(self, rows):
        self.revision = 0
        self.history = {0: {r['row_id']: r.copy() for r in rows}}
        self.last_write = {r['row_id']: 0 for r in rows}

    def snapshot(self, revision):
        return {k: v.copy() for k, v in self.history[revision].items()}

    def publish(self, writes):
        rows = self.snapshot(self.revision)
        self.revision += 1
        for key, value in writes.items():
            if value is None:
                rows.pop(key, None)
            else:
                rows[key] = value.copy()
            self.last_write[key] = self.revision
        self.history[self.revision] = rows
        return self.revision
