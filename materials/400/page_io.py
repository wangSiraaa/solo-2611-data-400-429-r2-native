"""Real positional file I/O, plus controlled barriers and one-shot failures."""
import os,threading
class Gate:
    def __init__(self):self.entered=threading.Event();self.allow=threading.Event()
    def release(self):self.allow.set()
class PageIO:
    page_size=128
    def __init__(self,path):
        self.fd=os.open(path,os.O_RDWR);self.page_count=os.fstat(self.fd).st_size//self.page_size
        self.lock=threading.Lock();self.gates={};self.failures=set();self.events=[];self.closed=False
    def pause_next(self,operation,page_id):
        gate=Gate()
        with self.lock:self.gates[(operation,page_id)]=gate
        return gate
    def fail_next(self,operation,page_id):
        with self.lock:self.failures.add((operation,page_id))
    def _before(self,operation,page_id):
        if not 0<=page_id<self.page_count:raise ValueError('page id out of range')
        with self.lock:
            gate=self.gates.pop((operation,page_id),None);fail=(operation,page_id) in self.failures
            self.failures.discard((operation,page_id));self.events.append({'event':'begin','operation':operation,'page_id':page_id,'thread':threading.current_thread().name})
        if gate is not None:gate.entered.set();gate.allow.wait()
        if fail:
            with self.lock:self.events.append({'event':'failed','operation':operation,'page_id':page_id})
            raise OSError('injected page I/O failure')
    def read_page(self,page_id):
        self._before('read',page_id);data=os.pread(self.fd,self.page_size,page_id*self.page_size)
        if len(data)!=self.page_size:raise OSError('short page read')
        with self.lock:self.events.append({'event':'complete','operation':'read','page_id':page_id,'bytes':len(data)})
        return data
    def write_page(self,page_id,data):
        if len(data)!=self.page_size:raise ValueError('full immutable page required')
        self._before('write',page_id);written=os.pwrite(self.fd,data,page_id*self.page_size)
        if written!=self.page_size:raise OSError('short page write')
        with self.lock:self.events.append({'event':'complete','operation':'write','page_id':page_id,'bytes':written})
    def inspect(self):
        with self.lock:return list(self.events)
    def close(self):
        if not self.closed:os.close(self.fd);self.closed=True
