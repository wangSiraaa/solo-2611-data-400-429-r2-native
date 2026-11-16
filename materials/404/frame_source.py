"""Decoded GraphQL incremental-response facility, deliberately no HTTP stack."""
import json
from pathlib import Path
class FrameSource:
    def __init__(self,path):self._frames=json.loads(Path(path).read_text());self._index=0;self.calls=0
    def read(self):
        self.calls+=1
        if self._index==len(self._frames):return None
        frame=self._frames[self._index];self._index+=1;return frame
