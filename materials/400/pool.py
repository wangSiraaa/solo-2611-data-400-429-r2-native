"""A serially correct pool whose metadata lock does not cover slow file I/O."""
from dataclasses import dataclass
import threading
@dataclass(eq=False)
class Frame:
    identity:int
    page_id:int
    data:bytearray
    pins:int=0
    dirty:bool=False
    generation:int=0
@dataclass(eq=False)
class Handle:
    frame:Frame
    active:bool=True
class BufferPool:
    def __init__(self,io,capacity):
        self.io=io;self.capacity=capacity;self.lock=threading.RLock();self.frames={};self.created=[];self.events=[]
        from writeback import Writeback
        self.writeback=Writeback(self)
    def fetch(self,page_id):
        victim=None
        with self.lock:
            if page_id in self.frames:
                frame=self.frames[page_id];frame.pins+=1;return Handle(frame)
            if len(self.frames)>=self.capacity:
                victim=next((f for f in self.frames.values() if f.pins==0),None)
                if victim is None:raise RuntimeError('all_pinned')
        if victim is not None:
            self.writeback.flush(victim.page_id)
            with self.lock:self.frames.pop(victim.page_id,None)
        data=self.io.read_page(page_id)
        with self.lock:
            frame=Frame(len(self.created),page_id,bytearray(data),pins=1);self.created.append(frame);self.frames[page_id]=frame
            self.events.append({'event':'loaded','page_id':page_id,'frame':frame.identity});return Handle(frame)
    def _check(self,handle,offset,length):
        if not handle.active:raise ValueError('released handle')
        if not 0<=offset<=offset+length<=self.io.page_size:raise ValueError('page bounds')
    def read(self,handle,offset=0,length=1):
        with self.lock:self._check(handle,offset,length);return bytes(handle.frame.data[offset:offset+length])
    def write(self,handle,offset,data):
        with self.lock:
            self._check(handle,offset,len(data));frame=handle.frame;frame.data[offset:offset+len(data)]=data;frame.generation+=1;frame.dirty=True
            self.events.append({'event':'modified','page_id':frame.page_id,'frame':frame.identity,'generation':frame.generation})
    def release(self,handle):
        with self.lock:
            self._check(handle,0,0);handle.active=False;handle.frame.pins-=1
    def flush(self,page_id):return self.writeback.flush(page_id)
    def capture(self,page_id):
        with self.lock:
            frame=self.frames.get(page_id)
            if frame is None or not frame.dirty:return None
            return frame,bytes(frame.data)
    def written(self,frame):
        with self.lock:
            frame.dirty=False
            self.events.append({'event':'written','page_id':frame.page_id,'frame':frame.identity,'current_generation':frame.generation})
    def inspect(self):
        with self.lock:return {'capacity':self.capacity,'frames':[{'page_id':p,'frame':f.identity,'pins':f.pins,'dirty':f.dirty,'generation':f.generation} for p,f in sorted(self.frames.items())],'created_frames':len(self.created),'events':list(self.events)}
    def close(self):
        with self.lock:
            if any(f.pins for f in self.created):raise RuntimeError('still_pinned')
            pages=list(self.frames)
        for page in pages:self.writeback.flush(page)
        self.io.close()
