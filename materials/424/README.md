# Branch-aware static data contract compiler

`probe.py` is the original smoke test. `verify.py` is the full verification
suite (counterexample replay, per-row oracles, contract coverage, structural
errors, CLI artefacts).

## What the compiler does

Each node carries a list of **shapes** — one per reachable row shape — instead
of one collapsed wide table. A shape maps field names to descriptors (runtime
type set plus an optional finite literal-value domain) and remembers its origin
(source node, variant index, concrete counterexample row) and the guards under
which it is reachable. Mutually exclusive fields therefore stay in separate
shapes and are never claimed to co-occur.

- `source`: one shape per declared variant; all variants are kept even if no
  sample data uses them.
- `where`: guards (`type_is`, `has`, `eq`, `and`, `or`, `not`) refine shapes;
  shapes proven false are dropped, so downstream never sees dead branches.
- `project`: every output column is analysed against every incoming shape.
  `if` refines each branch separately, so unreachable field accesses cannot
  cause false rejections. `get(field, default)` only analyses/uses the default
  when the field is absent — a present NULL is not defaulted.
- `concat`: keeps all input rows with their own shapes; nothing is widened.
- `assert_contract`: every reachable shape must be accepted by at least one
  expected variant. The check is over all shapes, not just supplied data.

`eq` is only allowed on finite-literal discriminant fields (source literal/NULL
declarations, renamed/passthrough copies of them, and constant projections);
using it on an ordinary int/text field or an `add`/`text_concat` result raises
`unsupported_guard`. `true` and `1` are distinct types; booleans are never
integers for `add`, and `text_concat` only accepts text.

Compile errors (`invalid_plan`, `unknown_reference`, `cycle`,
`unsupported_guard`, `missing_field`, `type_error`, `assert_failed`) carry the
node, expression path, originating shape, reachability guards and a concrete
legal source row that reproduces the failure.

## Running

```
python3 probe.py                       # original smoke test
python3 verify.py                      # 42-check verification suite
python3 pipeline.py PLAN --rows ROWS --out-dir DIR
```

The CLI writes three separate artefacts under `DIR`: `contract.json` (output
shapes plus provenance), `data.json` (business output rows), and
`diagnostics.json` (compile/run status). A failed compile writes only
diagnostics and never starts data processing. Source rows matching no declared
variant are rejected with their source id and 1-based row number.

## Cases under `cases/`

| directory          | focus |
|--------------------|-------|
| `literals`         | bool/int literals, `type_is`, literal-discriminant `eq` |
| `null_and_missing` | NULL vs missing field, `has`-guarded `get`, present NULL not defaulted |
| `concat_reproject` | two branches concat'd then reprojected behind an `eq`/`if` |
| `where_shared`     | `where` excludes shapes, shared node consumed by two downstreams |
| `assert_missing`   | `assert_contract` missing a reachable branch |
| `unsupported_guard`| `eq` over an open-domain int field |
| `type_error`       | bool handed to `add` |
| `bad_source_row`   | real rows violating declared variants with row numbers |
