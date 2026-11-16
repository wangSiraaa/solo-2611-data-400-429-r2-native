"""Actual independent page files; writes are atomic at the declared crash points."""
import json
import os
from pathlib import Path


class Pages:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit = []

    def read(self, page_id):
        return json.loads((self.root / (page_id + '.json')).read_text())

    def write(self, page_id, page):
        path = self.root / (page_id + '.json')
        temporary = path.with_suffix('.writing')
        with temporary.open('w') as output:
            json.dump(page, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.audit.append({'page_id': page_id, 'page_lsn': page['page_lsn'],
                           'operation': 'write_fsync_replace_directory_fsync'})
