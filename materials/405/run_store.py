import json
from pathlib import Path

class Store:
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / 'manifest.json'
        if not self.manifest.exists(): self.manifest.write_text('[]')
    def ids(self): return json.loads(self.manifest.read_text())
    def read(self, run_id):
        if run_id not in self.ids(): raise KeyError(run_id)
        return json.loads((self.root / (run_id + '.json')).read_text())
    def add(self, run_id, records):
        if not run_id.isalnum() or run_id in self.ids(): raise ValueError('run id')
        known={r['seq'] for x in self.ids() for r in self.read(x)}
        for r in records:
            if r['seq'] in known: raise ValueError('duplicate seq')
            known.add(r['seq'])
            if r['kind']=='put':
                if not isinstance(r['key'],int) or not isinstance(r['value'],str):raise ValueError('point')
            elif r['kind']=='erase':
                if r['lo']>=r['hi']:raise ValueError('empty range')
            else:raise ValueError('kind')
        (self.root/(run_id+'.json')).write_text(json.dumps(records))
        self.manifest.write_text(json.dumps(self.ids()+[run_id]))
    def replace(self, selected, output_id, records):
        ids=self.ids()
        if not set(selected)<=set(ids) or output_id in ids:raise ValueError('selection')
        (self.root/(output_id+'.json')).write_text(json.dumps(records))
        self.manifest.write_text(json.dumps([x for x in ids if x not in selected]+[output_id]))
