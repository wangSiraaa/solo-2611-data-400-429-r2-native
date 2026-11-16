class CoverIndex:
    def __init__(self):
        self.entries = {}
        self.examined = 0

    def on_write(self, record):
        rid = record['row_id']
        if record['deleted']:
            self.entries.pop(rid, None)
        else:
            self.entries[rid] = record.copy()

    def candidates(self, lo, hi):
        result = []
        for record in self.entries.values():
            self.examined += 1
            if lo <= record['category'] < hi:
                result.append(record.copy())
        return result
