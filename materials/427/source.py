import json
from pathlib import Path
class Source:
    def __init__(self,path): self.path=Path(path);self.decoded=0;self.scans=0
    def scan(self):
        self.scans+=1
        with self.path.open() as stream:
            for line in stream:
                self.decoded+=1;yield json.loads(line)
