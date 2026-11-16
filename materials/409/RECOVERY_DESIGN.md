# Recovery design: in-place delta pages with STEAL / NO-FORCE

This note explains the write-ahead log and recovery design implemented in
`engine.py`, `wal.py`, `page_io.py` and `recovery.py`.  It extends the existing
BEGIN/ADD/UNDO/COMMIT/ABORT contract with one new log record, `RECOVER_BEGIN`,
and keeps every previously persisted delta/UNDO record interpretable.

## Durability rules (the facilities)

* `Journal.append` only adds to an in-process pending buffer.  Nothing is
  crash-safe until `Journal.force` rewrites `wal.jsonl`, flushes and fsyncs it.
  `Journal.durable`/`durable_lsn` reopen the real file, so the reported durable
  prefix is what a fresh process would actually read.
* `Pages.write` fsyncs a temp file, atomically `os.replace`s it and fsyncs the
  directory.  Failures are only injected between these complete operations.
* Before every page write the engine forces the WAL through the page's
  `page_lsn` and re-reads the durable prefix to prove it covers that LSN.  The
  old unsafe state (a page ahead of the durable WAL) therefore cannot be
  produced.  If such a directory is handed to recovery, validation fails closed
  with exit code 2 rather than guessing a value.

## Engine semantics

* Business ADD deltas and every persisted row value are signed int64.  `bool`
  is rejected (it is not treated as an integer), and an ADD that would overflow
  is rejected as a whole: no log line, no lock, no page change.
* At most one open writer per `(page, key)`; a second writer gets `LOCKED`.
  Locks release when the transaction reaches a durable COMMIT or ABORT, so a
  later transaction can reuse the row (the `abort-reuse` case).
* STEAL: `flush` may write pages modified by still-active transactions.
  NO-FORCE: `commit` only forces the commit decision and required log records;
  dirty pages need not be on disk.  Transactions are never hidden in a private
  write set until commit, and flush never waits for a transaction to finish.

## Recovery

### Analysis
The durable prefix classifies each transaction: COMMIT = winner, ABORT =
already rolled back, anything still active = loser that this recovery aborts.

### Undo pass
For every loser ADD lacking a durable UNDO, append a physical UNDO using the
existing `delta/undo_of/undo_next` contract and **force it before any page IO**.
Then force an ABORT per loser.  Dying after the force but before the page write
is harmless: a repeated run finds the UNDO durable and the `undo_of` set
de-duplicates it, so a compensation is never written twice.

### Redo plan (`RECOVER_BEGIN`)
A naive "replay every record newer than page_lsn" cannot work here because
`page_lsn` is not a globally reliable membership test across recovery
boundaries: an interrupted recovery can leave a page whose `page_lsn` points at
a newer UNDO while an older never-flushed loser ADD is still absent, and a
crash page can contain an ADD whose UNDO was never flushed.  Re-deriving
membership from row values alone is underdetermined without the (deliberately
not retained) seed values.

Instead, before writing any recovery page, recovery appends and forces one
`RECOVER_BEGIN` record per affected page containing:

* `base_lsn` - the crash-time `page_lsn` the plan is built from,
* `apply_lsns` - the exact ordered physical LSNs to apply.

Plan membership relative to `base_lsn`:

* a winner ADD above base is a missing committed increment -> apply;
* a compensated loser ADD is never re-applied as a business change;
* an UNDO whose ADD is at or below base reached the crash page -> apply to
  erase that loser increment;
* an UNDO whose ADD is above base belongs to a pair that never reached the
  page -> skip (net zero).

The earliest durable plan per page is reused while no new physical ADD/UNDO was
appended after it, so an interrupted recovery always resumes the *same* plan.
Once later business transactions exist, old plans are stale and a fresh plan is
built from the current pages.

### Redo execution and idempotency
Each planned LSN is applied only if it exceeds the page's current `page_lsn`;
applying it raises `page_lsn` to that LSN.  Hence every physical change takes
effect at most once, even if an earlier run already wrote part of the result
and the process died mid-way.  Repeated `recover` calls converge to byte-equal
pages and then perform no further writes.

## Failure points

`main.py recover ROOT --fault NAME` stops on a real persistence boundary:

| fault              | exit | boundary                                              |
|--------------------|------|-------------------------------------------------------|
| `after-undo-log`   | 76   | an UNDO forced, its page write not done               |
| `after-abort`      | 77   | ABORT decision forced, redo not started               |
| `after-redo-write` | 75   | a recovery page write fsynced/replaced                |
| `before-ready`     | 78   | all recovery IO done, READY publication interrupted   |

The legacy integer `--crash-after N` maps 0 to `after-undo-log` and N>0 to the
Nth recovery page write.

## Cost comparison of the chosen organization

* **Log bytes.** One `RECOVER_BEGIN` per affected page per recovery generation.
  It stores an LSN list, so it is O(physical records touched) bytes rather than
  full before/after row images; it is also forced once and reused on resume.
  Keeping plain deltas/UNDOs (no per-ADD images) minimizes steady-state size.
* **Recovery re-reads / page writes.** The WAL prefix is read a small constant
  number of times (analysis, post-undo, post-plan).  Each page is written at
  most once per recovery regardless of how many records it replays, and a
  already-converged page is never rewritten.  Full per-page image logging would
  cap re-read work but cost a full page (up to 32 rows) per change.
* **Maintenance state.** No separate checkpoint/progress file or background
  coordinator is needed; all progress lives in the append-only WAL and is
  recovered by re-reading one directory.  The price is the plan record and the
  rule that stale plans are superseded when new physical records appear.

A full ARIES-style CLR table or per-change physical images would also be
correct, but either writes more per business change or needs an applied-LSN
table kept in sync; the single durable plan record gives provably repeatable
redo with the least extra machinery for this bounded protocol (<=8 pages,
<=32 keys/page, <=100 business ADDs, <=20 aborts).
