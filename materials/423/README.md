# Question 423 — persistent compound multikey query tool

The original three defects are fixed:

* index keys are extracted **per array element**, never as a cartesian product,
  so `[A/1, B/5]` can no longer generate the nonexistent `A/5` combination;
* `put` replaces a document together with every index posting in one SQLite
  transaction — old keys always leave before new keys enter;
* a real correlated planner distinguishes `elemMatch` (one element must
  satisfy every sub-condition) from plain `and` (different elements may
  satisfy different leaves).

## Components

| file | role |
| --- | --- |
| `indexer.py` | `IndexSpec` validation (cross-array indexes rejected up front) and correlated typed key extraction |
| `evaluator.py` | independent per-document / per-element interpreter; correctness oracle **and** residual filter |
| `planner.py` | JSON AST → access plan: prefix scans, posting intersect/union, root-gap expansion, justified full scans |
| `catalog.py` | persistent SQLite catalog: `build` / open / `put` / `remove` / `query` / `explain` / `verify`, access log |
| `cli.py` | command line front end |
| `tests/` | unit + differential + benchmark tests (stdlib `unittest`) and the deterministic generators |
| `probe.py` | end-to-end demonstration on `fixture.json` |
| `benchmark_probe.py` | 500-document selectivity benchmark (≤ 30 table fetches, access log retained) |

## Typed storage

Each indexed slot is `(type_tag, value)`. Tags are kept distinct for missing
(`M`), empty-array placeholder (`E`), explicit null (`N`), bool (`B`), int
(`I`) and text (`T`). Missing and null therefore never share an untyped
string; booleans never compare equal to integers. Empty and missing arrays
each get an explicit whole-array posting so a reader can see why a bound
cannot guarantee full coverage. Repeated identical keys inside one document
collapse to one posting.

## Correlation rule

* One scan may bound several `items.*` slots only when the bounds come from a
  single `elemMatch` block — then they describe one element.
* Independent `and` leaves get separate scans whose **document posting sets**
  are intersected; residual evaluation against the original AST guarantees no
  document is lost and no same-element combination is invented.
* An unservable `and` branch simply stays residual; an unservable `or` branch
  degrades to an explicitly justified full scan.
* Every plan records the real number of ranges, keys accessed, candidate
  documents, table fetches and residual checks. The explain is assembled from
  those counters — never from a precomputed answer.

## CLI

```bash
python3 cli.py build ./store indexes.json docs.json   # indexes: path list or name->paths
python3 cli.py put   ./store doc.json
python3 cli.py remove ./store <doc_id>
python3 cli.py query  ./store query.json
python3 cli.py explain ./store query.json
python3 cli.py verify ./store
python3 cli.py log    ./store
python3 cli.py dump   ./store <index_name>
```

Reopening the same directory sees consistent documents and indexes.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 probe.py
python3 benchmark_probe.py
```

The differential tests run hundreds of randomized queries through both the
catalog and the independent `evaluator` oracle, including reopen, `put`,
`remove`, duplicate elements, empty/missing arrays, explicit nulls, boolean
vs integer values and `or` branches.
