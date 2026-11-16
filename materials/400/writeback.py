"""Capture immutable bytes, release metadata lock, then clear dirty after I/O."""
class Writeback:
    def __init__(self,pool):self.pool=pool
    def flush(self,page_id):
        captured=self.pool.capture(page_id)
        if captured is None:return {'page_id':page_id,'written':False}
        frame,payload=captured
        self.pool.io.write_page(page_id,payload)
        self.pool.written(frame)
        return {'page_id':page_id,'written':True}
