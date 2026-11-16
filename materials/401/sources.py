"""Finite logical row sources with audited actual API row consumption."""
import json
from pathlib import Path
class Sources:
    def __init__(self,path):
        spec=json.loads(Path(path).read_text());self._left=spec['left'];self._right=spec['right'];self.estimate=spec['estimated_left_rows'];self._left_opened=False
        self.audit={'left_opens':0,'left_reads':0,'right_opens':0,'right_reads':0}
    def left(self):
        if self._left_opened:raise ValueError('left source is one-shot')
        self._left_opened=True;self.audit['left_opens']+=1
        for row in self._left:self.audit['left_reads']+=1;yield dict(row)
    def right(self):
        self.audit['right_opens']+=1
        for row in self._right:self.audit['right_reads']+=1;yield dict(row)
