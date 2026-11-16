import json,subprocess,sys
from pathlib import Path
class Dispatcher:
    def __init__(self,allocator,root):self.allocator=allocator;self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.children={};self.groups={}
    def reserve_one(self,stage,worker,replicas,allowed_nodes,resources):
        owner=stage+':'+str(worker)
        node=self.allocator.place_one(owner,resources,allowed_nodes)
        if node is None:return False
        request={'input':str(replicas[node]),'output':str(self.root/(owner.replace(':','-')+'.json')),'factor':worker+1}
        request_path=self.root/(owner.replace(':','-')+'-request.json');request_path.write_text(json.dumps(request))
        process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('worker.py')),str(request_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        assert process.stdout.readline().strip()=='READY'
        self.children[owner]=process;self.groups.setdefault(stage,[]).append(owner);return True
    def execute_group(self,stage):
        for owner in self.groups[stage]:self.children[owner].stdin.write('GO\n');self.children[owner].stdin.flush()
        results=[]
        for owner in self.groups[stage]:
            process=self.children[owner];results.append(json.loads(process.stdout.readline()));process.wait();self.allocator.release(owner)
        return results
    def cleanup(self):
        for owner,process in self.children.items():
            if process.poll() is None:process.terminate();process.wait()
            self.allocator.release(owner)
