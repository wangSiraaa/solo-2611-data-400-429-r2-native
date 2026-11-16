import json
from pathlib import Path
from inverted import Index
from scorer import statistics
from profile_query import query, score

case = json.loads(Path(__file__).with_name('profile-cases.json').read_text())
docs = {'A': ['alpha'] * 3, 'B': ['alpha'] * 4 + ['pad'] * 196}
docs.update({f'C{i:03d}': ['beta'] * 200 for i in range(98)})
index = Index(docs, case['block_size'])
stats = statistics(docs)
checks = []
for profile in case['profiles']:
    parameters = {key: profile[key] for key in ('k1', 'b')}
    actual, skipped = query(index, case['terms'], case['k'], **parameters)
    oracle = sorted([(identity, score(case['terms'], tokens, stats, **parameters))
                     for identity, tokens in docs.items() if 'alpha' in tokens],
                    key=lambda pair: (-pair[1], pair[0].encode()))[:1]
    assert oracle[0][0] == profile['expected_winner']
    # Fixed-case feasibility only: the 98 non-alpha blocks have an exact zero
    # alpha bound from their posting metadata; scoring A and B suffices.
    exact_zero_blocks = sum('alpha' not in tokens for tokens in docs.values())
    checks.append(dict(parameters=parameters, actual=actual, oracle=oracle,
                       skipped=skipped, metadata_zero_blocks=exact_zero_blocks,
                       matching_document_count=2))
assert checks[0]['actual'] == checks[0]['oracle']
assert checks[1]['actual'] != checks[1]['oracle']
print(json.dumps({'checks': checks,
                  'scope': 'Real configurable caller counterexample plus fixed-input posting feasibility; no general repaired engine.'}))
