from heap import Heap
from cover_index import CoverIndex


class Engine:
    def __init__(self):
        self.heap = Heap()
        self.index = CoverIndex()

    def put(self, rid, category, amount):
        self.index.on_write(self.heap.put(rid, category, amount))

    def delete(self, rid):
        self.index.on_write(self.heap.delete(rid))

    def snapshot(self):
        return self.heap.commit

    def query(self, snapshot, lo, hi, projection):
        rows = self.index.candidates(lo, hi)
        rows.sort(key=lambda r: (r['category'], r['row_id'].encode()))
        return [[r[col] for col in projection] for r in rows]
