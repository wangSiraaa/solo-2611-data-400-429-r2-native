import json
from pathlib import Path
from partial_index import PartialIndex
from prepared_client import prepare, execute

case = json.loads(Path(__file__).with_name('prepared-cases.json').read_text())
rows = {row['row_id']: row for row in case['rows']}


def index(predicate):
    result = PartialIndex(predicate)
    for row in rows.values(): result.put(row)
    return result


catalog = {'live': index(case['initial_partial'])}
handle = prepare(case['query'], 'live', catalog)
before = [row['row_id'] for row in execute(handle, rows, catalog)]
catalog['live'] = index(case['replacement_partial'])
after = [row['row_id'] for row in execute(handle, rows, catalog)]
assert before == ['a', 'b'] and after == ['b']
print(json.dumps({'baseline_before_replacement': before, 'baseline_after_replacement': after,
                  'required_result_ids': case['expected_result_ids'],
                  'scope': 'Runs current index and cached-path caller; target revalidation is absent.'}))
