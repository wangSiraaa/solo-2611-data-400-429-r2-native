"""Correct ordinary nested loop; prototype switch restarts already emitted work."""
import itertools
from sources import Sources
from planner import Planner

def matches(left,right):return left['key'] is not None and right['key'] is not None and left['key']==right['key']
def pair(left,right):return {'left_id':left['id'],'right_id':None if right is None else right['id'],'left_value':left['value'],'right_value':None if right is None else right['value']}
class Executor:
    def __init__(self,path):self.sources=Sources(path);self.planner=Planner(self.sources.estimate);self._iterator=self._run();self.closed=False
    def _run(self):
        left_iter=iter(self.sources.left());seen=[]
        for left in left_iter:
            seen.append(left);matched=False
            for right in self.sources.right():
                if matches(left,right):
                    matched=True;yield pair(left,right);self.planner.on_pair()
                    if self.planner.choose()=='hash':
                        index={}
                        for item in self.sources.right():
                            if item['key'] is not None:index.setdefault(item['key'],[]).append(item)
                        for replayed in itertools.chain(seen,left_iter):
                            found=index.get(replayed['key'],[]) if replayed['key'] is not None else []
                            for item in found:yield pair(replayed,item)
                            if not found:yield pair(replayed,None)
                        return
            if not matched:yield pair(left,None)
    def next(self):
        if self.closed:return None
        try:return next(self._iterator)
        except StopIteration:self.closed=True;return None
    def inspect(self):return {'source':dict(self.sources.audit),'decisions':list(self.planner.decisions),'complete':self.closed}
