"""Exhaustive correctness verification against an independent oracle.

Covers, on both default and random valid (k1,b) configurations:
  * static ranking/score equality with the full-scoring oracle;
  * deletion of common-term documents, long->short and short->long edits,
    same-id term replacement, delete-and-reinsert, full empty corpus,
    k larger than the number of hits, exact-tie UTF-8 doc_id ordering;
  * bound soundness: every skipped block's exported upper bound is a true
    upper bound over every active document in that block;
  * persistence: save/reopen returns identical results and statistics;
  * invalid parameters are rejected without modifying the index;
  * the public benchmark/profile budgets (nonempty skips, <50 accesses).
"""
import json
import math
import random
import tempfile
from pathlib import Path

from inverted import Index
from oracle import oracle
from ranker import query
from inspect import inspect as inspect_index
from envelopes import block_upper_bound
from scorer import statistics, score_p, validate_params

WORDS = ['alpha', 'beta', 'gamma', 'delta', 'pad', 'x', 'zzz', '字节词']
PASS, FAIL = 0, 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f'  FAIL: {name} {detail}')


def random_docs(rng, count, max_len=25):
    docs = {}
    for i in range(count):
        length = rng.randint(0, max_len)
        docs[f'D{i:03d}'] = [rng.choice(WORDS) for _ in range(length)]
    return docs


def assert_bound_soundness(index, terms, k1, b):
    """No skipped (or retained) block may carry an under-estimated bound."""
    stats = index.current_stats()
    for block, envelope in zip(index.blocks, index.envelopes):
        upper = block_upper_bound(terms, envelope, stats, k1, b)
        for doc_id in block:
            tokens = index.tokens_of(doc_id)
            real = score_p(terms, tokens, stats, k1, b)
            # tiny tolerance for the explicit rounding slack direction
            if not (upper + 1e-9 >= real):
                return False, (block, doc_id, upper, real)
    return True, None


def run_sequence(label, seed, configs):
    rng = random.Random(seed)
    docs = random_docs(rng, rng.randint(1, 60))
    index = Index(docs, block_size=rng.choice([1, 2, 3, 5, 8]))
    terms_pool = list(dict.fromkeys(
        t for toks in docs.values() for t in toks)) or ['alpha']

    def pick_terms():
        r = rng.sample(terms_pool, min(rng.randint(1, 4), len(terms_pool)))
        return r

    for step in range(45):
        terms = pick_terms()
        k = rng.randint(1, 10)
        for k1, b in configs:
            rows, audit = query(index, terms, k, k1=k1, b=b)
            orows, oinfo = oracle(index, terms, k, k1=k1, b=b)
            check(f'{label} s{step} ranking p=({k1},{b})', rows == orows,
                  f'{rows} != {orows}')
            # maintained statistics must equal independently recomputed ones
            st = index.current_stats()
            check(f'{label} s{step} stats N', st['n'] == oinfo['n'])
            check(f'{label} s{step} stats avgdl',
                  abs(st['avgdl'] - oinfo['avgdl']) < 1e-12)
            for t in terms:
                check(f'{label} s{step} df {t}',
                      st['df'].get(t, 0) == oinfo['df'][t])
            sound, detail = assert_bound_soundness(index, terms, k1, b)
            check(f'{label} s{step} bound soundness p=({k1},{b})', sound,
                  str(detail))
            check(f'{label} s{step} raw<=scored',
                  audit['raw_document_accesses'] == audit['scored'])

        # mutate: put / remove / replace / reinsert
        action = rng.random()
        ids = index.active_ids()
        if action < 0.35 and ids:                       # replacement/resize
            doc_id = rng.choice(ids)
            new_len = rng.randint(0, 25)
            index.put(doc_id, [rng.choice(WORDS) for _ in range(new_len)])
        elif action < 0.55 and ids:                     # removal
            index.remove(rng.choice(ids))
        elif action < 0.75:                             # fresh insertion
            index.put(f'NEW{step:02d}_{rng.randint(0,9999)}',
                      [rng.choice(WORDS)
                       for _ in range(rng.randint(0, 25))])
        elif ids:                                       # same-id term swap
            doc_id = rng.choice(ids)
            old = index.tokens_of(doc_id)
            swapped = [('gamma' if t == 'alpha' else 'alpha') if t in
                       ('alpha', 'gamma') else t for t in old]
            index.put(doc_id, swapped)
        # refresh term pool from active metadata
        terms_pool = list(index.df) or ['alpha']


def test_scenarios():
    # 1. common-term document deletion (mirrors the demo shape, general terms)
    docs = {f'C{i:02d}': ['beta'] * 10 for i in range(30)}
    docs['A'] = ['alpha']
    docs['B'] = ['beta'] * 4 + ['alpha']
    index = Index(docs, block_size=4)
    for i in range(25):
        index.remove(f'C{i:02d}')
    for k1, b in [(1.2, 0.75), (2.0, 0.0), (0.5, 0.3)]:
        rows, _ = query(index, ['alpha', 'beta'], 3, k1=k1, b=b)
        orows, _ = oracle(index, ['alpha', 'beta'], 3, k1=k1, b=b)
        check('common-term deletion ranking', rows == orows)

    # 2. long -> short and short -> long on the same doc id
    index = Index({'L': ['w'] * 50, 'S': ['w']}, block_size=1)
    index.put('L', ['w'])                 # long becomes short
    index.put('S', ['w'] * 50)            # short becomes long
    rows, _ = query(index, ['w'], 2)
    orows, _ = oracle(index, ['w'], 2)
    check('length swap ranking', rows == orows, f'{rows} vs {orows}')

    # 3. same-id term replacement changes df/postings
    index = Index({'D': ['alpha', 'alpha', 'beta']}, block_size=1)
    index.put('D', ['gamma'])
    check('posting removed after replace',
          'alpha' not in index.postings and 'beta' not in index.postings
          and index.df.get('gamma') == 1)
    rows, _ = query(index, ['alpha'], 5)
    check('replaced doc no longer hits old term', rows == [])

    # 4. delete then reinsert same id
    index = Index({'D': ['alpha'], 'E': ['alpha', 'beta']}, block_size=1)
    index.remove('D')
    index.put('D', ['beta'])
    rows, _ = query(index, ['alpha', 'beta'], 5)
    orows, _ = oracle(index, ['alpha', 'beta'], 5)
    check('delete-reinsert ranking', rows == orows)

    # 5. full empty corpus
    index = Index({'D': ['alpha'], 'E': ['beta']}, block_size=1)
    index.remove('D')
    index.remove('E')
    st = index.current_stats()
    check('empty N', st['n'] == 0 and st['avgdl'] == 0.0
          and st['total_length'] == 0 and index.df == {})
    rows, audit = query(index, ['alpha'], 3)
    check('empty corpus query', rows == [] and audit['scored'] == 0
          and audit['raw_document_accesses'] == 0)

    # empty documents still count in N / average length
    index = Index({'E': [], 'F': ['alpha', 'alpha']}, block_size=1)
    rows, audit = query(index, ['alpha'], 5)
    orows, _ = oracle(index, ['alpha'], 5)
    check('empty doc counted in N/avgdl',
          audit['statistics_used']['n'] == 2 and rows == orows)

    # 6. k larger than number of hits
    index = Index({f'D{i}': ['alpha'] for i in range(3)}, block_size=2)
    rows, _ = query(index, ['alpha'], 20)
    check('k>hits returns all hits', len(rows) == 3)
    rows2, _ = query(index, ['nomatch'], 5)
    check('no-hit term', rows2 == [])

    # 7. exact tie -> UTF-8 byte order
    docs = {'b': ['z'], 'a': ['z'], '字节': ['z'], 'A': ['z'], '中': ['z']}
    index = Index(docs, block_size=2)
    rows, _ = query(index, ['z'], 10)
    expected_order = sorted(docs, key=lambda d: d.encode('utf-8'))
    check('tie utf-8 order', [r[0] for r in rows] == expected_order,
          str([r[0] for r in rows]))
    orows, _ = oracle(index, ['z'], 10)
    check('tie scores equal oracle', rows == orows)

    # 8. invalid parameters rejected, index untouched
    before = inspect_index(index)
    for bad_k1, bad_b in [(0.05, 0.75), (3.1, 0.5), (1.2, -0.1),
                          (1.2, 1.1), (float('nan'), 0.5),
                          (float('inf'), 0.5), (1.2, float('nan'))]:
        try:
            query(index, ['z'], 3, k1=bad_k1, b=bad_b)
            check(f'reject params {bad_k1},{bad_b}', False)
        except ValueError:
            check(f'reject params {bad_k1},{bad_b}', True)
    after = inspect_index(index)
    check('index unchanged after rejected params',
          before['revision'] == after['revision']
          and before['df'] == after['df'])


def test_persistence():
    docs = {'A': ['alpha'] * 3, 'B': ['alpha', 'pad'] * 2,
            'C': ['beta'] * 4}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'idx'
        index = Index(docs, block_size=2, store_path=str(path))
        index.put('D', ['alpha', 'gamma'])
        index.remove('C')
        rows1, audit1 = query(index, ['alpha', 'gamma'], 5, k1=2, b=0)
        index.save()

        reopened = Index.open(str(path))
        rows2, audit2 = query(reopened, ['alpha', 'gamma'], 5, k1=2, b=0)
        check('reopen identical rows', rows1 == rows2)
        check('reopen identical stats',
              audit1['statistics_used'] == audit2['statistics_used'])
        check('reopen revision', audit1['revision'] == audit2['revision'])
        # payloads survive reopen and reads are still metered
        check('reopen payload readable',
              reopened.tokens_of('A') == ['alpha'] * 3)
        rep = inspect_index(reopened, terms=['alpha'])
        check('reopen inspect currency',
              rep['statistics_applicability']['current'] is True)


def test_public_budgets():
    case = json.loads(Path(__file__).with_name('benchmark.json').read_text())
    index = Index(case['docs'], case['block_size'])
    rows, audit = query(index, case['terms'], case['k'])
    check('benchmark winner', rows[0][0] == case['expected_id'])
    check('benchmark skips nonempty block',
          audit['skipped_nonempty_blocks'] >= 1)
    check('benchmark scored<50', audit['scored'] < 50)
    check('benchmark raw<50', audit['raw_document_accesses'] < 50)

    pcase = json.loads(Path(__file__).with_name(
        'profile-cases.json').read_text())
    docs = {'A': ['alpha'] * 3, 'B': ['alpha'] * 4 + ['pad'] * 196}
    docs.update({f'C{i:03d}': ['beta'] * 200 for i in range(98)})
    index = Index(docs, pcase['block_size'])
    for profile in pcase['profiles']:
        rows, audit = query(index, pcase['terms'], pcase['k'],
                            k1=profile['k1'], b=profile['b'])
        orows, _ = oracle(index, pcase['terms'], pcase['k'],
                          k1=profile['k1'], b=profile['b'])
        check(f"profile {profile['k1']},{profile['b']} winner",
              rows[0][0] == profile['expected_winner'] and rows == orows)
        check('profile skips nonempty block',
              audit['skipped_nonempty_blocks'] >= 1)
        check('profile raw<50', audit['raw_document_accesses'] < 50)
        check('profile scored<50', audit['scored'] < 50)


def test_config_switch_same_index():
    # switch params repeatedly, then edit a doc, then switch again
    docs = {'A': ['alpha'] * 3, 'B': ['alpha'] * 4 + ['pad'] * 196}
    docs.update({f'C{i:03d}': ['beta'] * 200 for i in range(98)})
    index = Index(docs, 1)
    winners = []
    for k1, b in [(1.2, 0.75), (2.0, 0.0), (1.2, 0.75), (0.1, 1.0),
                  (2.0, 0.0)]:
        rows, audit = query(index, ['alpha'], 1, k1=k1, b=b)
        orows, _ = oracle(index, ['alpha'], 1, k1=k1, b=b)
        check('repeated switch correct', rows == orows)
        winners.append(rows[0][0])
    # make the short doc long and the long doc short, then switch again
    index.put('A', ['alpha'] * 3 + ['pad'] * 197)
    index.put('B', ['alpha'] * 4)
    for k1, b in [(1.2, 0.75), (2.0, 0.0)]:
        rows, _ = query(index, ['alpha'], 1, k1=k1, b=b)
        orows, _ = oracle(index, ['alpha'], 1, k1=k1, b=b)
        check('post-edit switch correct', rows == orows)


def main():
    test_scenarios()
    test_persistence()
    test_public_budgets()
    test_config_switch_same_index()
    run_sequence('random-default', 417, [(1.2, 0.75)])
    run_sequence('random-configs', 991,
                 [(1.2, 0.75), (2.0, 0.0), (0.1, 1.0), (1.5, 0.3), (3.0, 0.5)])
    print(f'\n{PASS} checks passed, {FAIL} failed')
    if FAIL:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
