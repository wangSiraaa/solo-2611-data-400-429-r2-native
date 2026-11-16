import json
from pathlib import Path
class ChunkSource:
    def __init__(self,manifest):
        self.manifest=Path(manifest);self.spec=json.loads(self.manifest.read_text());self.rows_read=0;self.files_read=0
    def batches(self):
        for filename in self.spec['chunks']:
            path=self.manifest.parent/filename
            rows=json.loads(path.read_text());self.files_read+=1;self.rows_read+=len(rows)
            yield rows
