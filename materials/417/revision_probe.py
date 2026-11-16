"""Profile-switching verification on profile-cases.json.

Both configurations are checked against an independent full-scoring oracle on
the same reopened-capable index. Each switch must (a) produce the oracle
ranking under its own k1/b, (b) still skip nonempty blocks on this selective
input, and (c) stay under the raw-access/scored budget. A bound qualified for
the default configuration can never be reused for k1=2,b=0.
"""
import json
from pathlib import Path

from oracle import oracle
from profile_query import query_audited
from inverted import Index


def build_case_docs():
    docs = {'A': ['alpha'] * 3,
            'B': ['alpha'] * 4 + ['pad'] * 196}
    docs.update({f'C{i:03d}': ['beta'] * 200 for i in range(98)})
    return docs


def run():
    case = json.loads(Path(__file__).with_name(
        'profile-cases.json').read_text())
    index = Index(build_case_docs(), case['block_size'])
    checks = []
    for profile in case['profiles']:
        params = {key: profile[key] for key in ('k1', 'b')}
        rows, audit = query_audited(index, case['terms'], case['k'], **params)
        oracle_rows, oracle_info = oracle(
            index, case['terms'], case['k'], **params)
        budget = case['max_query_raw_document_access_exclusive']
        checks.append({
            'parameters': params,
            'expected_winner': profile['expected_winner'],
            'actual': rows,
            'oracle': oracle_rows,
            'matches_oracle': rows == oracle_rows,
            'bound_basis': {
                'revision': audit['revision'],
                'k1': audit['parameters']['k1'],
                'b': audit['parameters']['b'],
                'statistics_used': audit['statistics_used'],
            },
            'skipped_nonempty_blocks': audit['skipped_nonempty_blocks'],
            'scored': audit['scored'],
            'raw_document_accesses': audit['raw_document_accesses'],
            'under_budget': audit['raw_document_accesses'] < budget
            and audit['scored'] < case['max_scored_exclusive'],
        })
    assert all(c['matches_oracle'] for c in checks)
    assert all(c['skipped_nonempty_blocks'] >= 1 for c in checks)
    assert all(c['under_budget'] for c in checks)
    # The two configurations really do rank differently, and each answers from
    # its own qualified bounds rather than a cached top-k.
    assert checks[0]['actual'][0][0] != checks[1]['actual'][0][0]
    return {
        'checks': checks,
        'scope': 'Same index, consecutive parameter switches; per-query bound '
                 'export qualified by revision+k1+b; oracle verified.',
    }


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
