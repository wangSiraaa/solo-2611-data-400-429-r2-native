import json
from pathlib import Path
from page_writer import encode
case=json.loads(Path('benchmark.json').read_text())
rows=[(bytes.fromhex(r['key']),bytes.fromhex(r['value'])) for r in case['records']]
page=encode(rows,case['restart'])
raw=json.dumps(page,sort_keys=True,separators=(',',':')).encode()
group=encode(rows[24:32],case['restart'])
assert len(raw)<=case['page_bytes'] and len(group['records'])<=16
print(json.dumps(dict(actual_encoded_page_bytes=len(raw),restart_groups=len(page['directory']),
                     isolated_group_records=len(group['records']),
                     scope='Existing encoder verifies input fit and group-local re-encoding feasibility; update solution absent.')))
