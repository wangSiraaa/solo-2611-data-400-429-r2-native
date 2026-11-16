from predicate import evaluate


class PartialIndex:
    def __init__(self,predicate):
        self.predicate=predicate
        self.entries={}

    def put(self,row):
        if evaluate(self.predicate,row) is True:
            self.entries[row['row_id']]=row.copy()

    def delete(self,rid):
        self.entries.pop(rid,None)

    def scan(self):
        return sorted(self.entries.values(),key=lambda r:(r['x'] is not None,
                      0 if r['x'] is None else r['x'],r['row_id'].encode()))
