import json,shutil,sys,tempfile,threading
from pathlib import Path
from page_io import PageIO
from pool import BufferPool
FIXTURE=Path(__file__).parent/'fixtures/pages.bin'
def worker(name,fn):
    outcome={}
    def run():
        try:outcome['value']=fn()
        except BaseException as e:outcome['error']=str(e)
    thread=threading.Thread(target=run,name=name);thread.start();return thread,outcome
def wait(event):
    if not event.wait(3):raise RuntimeError('controlled schedule did not reach its event')
def join(thread):
    thread.join(3)
    if thread.is_alive():raise RuntimeError('worker remains blocked after gate release')
def disk_byte(path,page):
    with open(path,'rb') as f:f.seek(page*128);return f.read(1).hex()
def scenario(name):
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'pages.bin';shutil.copyfile(FIXTURE,path);io=PageIO(path);pool=BufferPool(io,2);handles=[];threads=[];gates=[]
        try:
            if name=='normal':
                a=pool.fetch(0);b=pool.fetch(0);handles.extend([a,b]);pool.write(a,0,b'X');before=pool.read(b).hex();pool.writeback.flush(0)
                result={'same_frame':a.frame is b.frame,'second_handle':before,'disk':disk_byte(path,0)}
            elif name=='double-load':
                gate=io.pause_next('read',0);gates.append(gate)
                first,fo=worker('first-fetch',lambda:pool.fetch(0));threads.append(first);wait(gate.entered)
                second,so=worker('second-fetch',lambda:pool.fetch(0));threads.append(second)
                # The gate is released after checking whether the second caller wrongly completed.
                second.join(.1);second_returned_before_first=not second.is_alive()
                if 'value' in so:pool.write(so['value'],0,b'Y')
                gate.release();join(first);join(second)
                for item in (fo,so):
                    if 'value' in item:handles.append(item['value'])
                a=fo.get('value');b=so.get('value');c=pool.fetch(0);handles.append(c)
                result={'second_returned_while_first_read_blocked':second_returned_before_first,'first':None if a is None else pool.read(a).hex(),'second':None if b is None else pool.read(b).hex(),'new_fetch':pool.read(c).hex(),'same_frame':a is not None and b is not None and a.frame is b.frame is c.frame}
            elif name=='dirty-during-flush':
                a=pool.fetch(0);handles.append(a);pool.write(a,0,b'X');gate=io.pause_next('write',0);gates.append(gate)
                flush,fo=worker('flush-older-generation',lambda:pool.writeback.flush(0));threads.append(flush);wait(gate.entered);pool.write(a,0,b'Y');gate.release();join(flush)
                after_first=pool.inspect();second=pool.writeback.flush(0)
                result={'first_flush':fo,'after_first':after_first,'second_flush':second,'memory':pool.read(a).hex(),'disk':disk_byte(path,0)}
            elif name=='overlapping-flush':
                a=pool.fetch(0);handles.append(a);pool.write(a,0,b'X');gate=io.pause_next('write',0);gates.append(gate)
                first,fo=worker('first-flush',lambda:pool.writeback.flush(0));threads.append(first);wait(gate.entered);pool.write(a,0,b'Y')
                second,so=worker('second-flush',lambda:pool.writeback.flush(0));threads.append(second);second.join(.1);second_returned=not second.is_alive()
                gate.release();join(first);join(second)
                result={'second_returned_before_older_io':second_returned,'first':fo,'second':so,'memory':pool.read(a).hex(),'disk':disk_byte(path,0)}
            elif name=='unrelated-progress':
                a=pool.fetch(0);b=pool.fetch(1);handles.extend([a,b]);pool.write(a,0,b'X');gate=io.pause_next('write',0);gates.append(gate)
                flush,fo=worker('paused-flush',lambda:pool.writeback.flush(0));threads.append(flush);wait(gate.entered);progress=threading.Event()
                def other():
                    h=pool.fetch(1)
                    try:pool.write(h,0,b'Z');value=pool.read(h).hex();progress.set();return value
                    finally:pool.release(h)
                other_thread,oo=worker('unrelated-resident-page',other);threads.append(other_thread);completed=progress.wait(3);gate.release();join(flush);join(other_thread)
                result={'unrelated_progress_before_io_release':completed,'other':oo,'flush':fo}
            else:raise ValueError('unknown scenario')
            result['pool']=pool.inspect();result['io']=io.inspect();return result
        finally:
            for gate in gates:gate.release()
            for thread in threads:thread.join(3)
            for handle in handles:
                if handle.active:pool.release(handle)
            pool.close()
if __name__=='__main__':
    names=['normal','double-load','dirty-during-flush','overlapping-flush','unrelated-progress'] if len(sys.argv)==1 or sys.argv[1]=='all' else [sys.argv[1]]
    print(json.dumps({name:scenario(name) for name in names},ensure_ascii=False))
