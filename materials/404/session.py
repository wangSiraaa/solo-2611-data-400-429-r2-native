from frame_source import FrameSource
from reader import step
from materializer import Relations
from sqlite_view import SQLiteView
import tempfile
class Session:
    def __init__(self,path,db_path=None):
        self._temp=tempfile.TemporaryDirectory() if db_path is None else None
        self.db_path=str(db_path) if db_path is not None else self._temp.name+'/preview.sqlite'
        self.source=FrameSource(path);self.state={};self.relations=Relations();self.status='new'
        self.view=SQLiteView(self.db_path);self.frame=0;self.publish_hook=None
    def pump(self):
        response=step(self.source,self.state)
        if response is None:return self.query()
        self.relations.apply(response)
        self.status='complete' if response['complete'] else 'receiving'
        self.frame+=1
        self.view.publish({'status':self.status,'last_frame':self.frame,
                           'errors':response['errors'],**self.relations.query()},self.publish_hook)
        return self.query()
    def query(self):return self.view.query()
