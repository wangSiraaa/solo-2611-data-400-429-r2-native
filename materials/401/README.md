# Adaptive LEFT JOIN pipeline

    python3 main.py fixtures/normal.json
    python3 main.py fixtures/fanout.json
    python3 main.py fixtures/sparse.json
    python3 main.py fixtures/nulls.json

    python3 test_adaptive.py        # independent oracle + cost-contract checks

Uses Python 3.11 only. Sources.left is one-shot; Sources.right may be rescanned
and is immutable. Application source access uses those iterators, not private
fixture arrays. Audit counts actual API row visits, not physical disk reads or
file JSON decoding. A right row consumed during nested probing, hash build,
discarded work or rescan counts each time. Cache/hash probes are reported
separately and are not source row visits. A row returned by Executor.next is
immediately visible; later plan changes cannot retract it. Source occurrence id
is unique on its side; equal keys/values with different ids remain different
occurrences. NULL never matches NULL. LEFT JOIN produces every matching pair or
one null-extended left row if there are no matches. Source order defines left
order and right order within each left. This is not an external-memory join or
a general cost-based SQL optimizer.

## What was fixed

The starter had two independent runtime mistakes:

1. **Wrong work signal.** The planner was fed the number of *emitted output
   pairs* as a proxy for nested-loop scan work. Sparse joins emit almost
   nothing while still paying a full right scan per left row, so the planner
   never switched. The planner is now driven by real source telemetry: total
   `right_reads`, the size of the last full right scan, completed left rows,
   matched/unmatched left rows, and pairs emitted. The seed
   `estimated_left_rows` is only an initial projection; measured completed rows
   correct it, and the estimate is never treated as ground truth.
2. **Replay after switching.** The starter replayed `seen + remaining left
   iterator` through a fresh index, re-emitting already-published pairs and
   re-scanning an already-finished left row. Switching now happens at a
   **left-row boundary** (see below) and only unconsumed left rows enter the
   hash path.

The switch is a real physical change: the executor builds a right-side hash
index from one full `Sources.right` pass and, for every subsequent left row,
uses only that index. The planner records `chosen: "hash"`,
`reason`, the triggering observation and the cost projection in
`inspect()["decisions"]`.

## Handoff: row boundary (not mid-row frontier migration)

The planner is consulted exactly once per left-row boundary, after the current
left row's right scan has either matched or been exhausted and before the next
left row is processed. Ownership at that instant:

| state | owner | guarantee |
|---|---|---|
| published pairs | caller | never retracted, never replayed |
| finished left row | executor | match flag final; NULL extension emitted at the boundary; never re-scanned |
| unconsumed left rows | one-shot `Sources.left` iterator | exactly the rows hash mode consumes next |
| right index | executor | built by one full immutable right scan at the boundary |

Cost of this choice: one extra full right pass (the build). In exchange no
per-row match cursor is saved, no published pair is replayed, and the completed
left row is not re-scanned. A mid-row **frontier migration** would save at most
one partial scan, but it must carry "how many of this row's matches are already
published" and "did this row match at all" across the operator change. The row
boundary makes both facts final before the strategy changes, which is why the
implementation uses it.

`inspect()` exposes both telemetry layers so they can be cross-checked:

* `source.right_reads` / `right_opens` — logical source row visits (nested
  scans + build).
* `switch.right_reads_before_switch` + `switch.build_source_right_reads` — the
  same total decomposed; `reconcile.source_reads_match` asserts they add up.
* `probes.index_lookups` / `probes.index_probes` — in-memory index work, kept
  out of the source audit.

## Measured results

| fixture | rows | matches | first visible (left/right reads) | final right_reads | switch |
|---|---|---|---|---|---|
| normal | 1 | 1 | 1 / 1 | 3 | none (no remaining work) |
| fanout | 35 | 16 | 1 / 1 | 128 | after left row 1, build 64 |
| sparse | 20 | 1 | 1 / 1 | 128 | after left row 1, build 64 |
| nulls | 5 | 4 | 1 / 3 | 6 | after left row 1, build 3 |

Both camapaign fixtures on fresh instances: first `next()` visible at
`left_reads == 1` and `right_reads <= 4`, full run `right_reads <= 256`, exactly
one runtime strategy switch while unprocessed left rows remain. `nulls`
exercises a NULL left row (one null extension, no false match), two left rows
with the same key (each independently matched) and two right rows with equal
value but distinct ids (both appear).

`test_adaptive.py` also runs 300 randomized inputs with shuffled left/right
order, varying initial estimates (0 … 10^9) and key distributions, plus 30×64 /
128-codepoint boundary stress, all-hot fan-out (1920 pairs, no duplicates),
under/over-estimates and empty inputs, comparing the full `(left_id, right_id)`
identity sequence against a direct enumeration oracle.
