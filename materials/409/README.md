# In-place delta pages and transaction recovery

Run the end-to-end probe with `python3 -B probe.py` and the independent
model-based verification with `python3 -B model_tester.py`.  The probe copies
real seed pages, runs schedules in child processes killed with `os._exit(73)`,
and reopens the resulting files in fresh recovery processes.  Work directories
are disposable; the seed is initialization material only, never an input to
`recover(ROOT)`.

## Entries

* `python3 main.py script ROOT SCHEDULE` - run
  begin/add/commit/abort/flush/crash.  Construction runs restart recovery first,
  so business entries only open once historical transactions are resolved.
* `python3 main.py recover ROOT [--fault NAME | --crash-after N]` - restart
  recovery; prints a READY report or fails closed (exit 2) on an unrecoverable
  directory.
* `python3 main.py inspect ROOT` - read-only durable-state view (WAL prefix,
  pages, transaction states, compensation and redo-plan records); never writes.

## What is guaranteed

* WAL-before-page: every page write is preceded by a real, reopenable force of
  the WAL prefix through the page's `page_lsn`.  An old-style directory whose
  `page_lsn` is beyond the durable WAL end is rejected, not guessed at.
* STEAL and NO-FORCE are preserved: uncommitted pages may be flushed, and a
  successful commit only requires its decision and necessary log records to be
  durable.
* On READY, every durably committed increment is in effect exactly once;
  transactions without a durable commit leave no net business effect; a normal
  abort followed by a new committed write to the same row keeps the new value.
* Recovery is repeatable.  Stopping after an UNDO is forced but before its page
  write, or after some undo pages are written, resumes to the same result with
  no double add/subtract and no loss of another transaction's same-page change.
  Durable redo plans (`RECOVER_BEGIN`, see `RECOVERY_DESIGN.md`) make this
  provably idempotent even when `page_lsn` cannot alone identify membership.
* Business ADD deltas and persisted row values stay in signed int64; booleans
  are rejected and overflowing ADDs are rejected whole.  Internal UNDO inverse
  deltas use mathematical integers (including +2^63), while every final written
  row value remains int64.

`diagnostic_undo.py` is unchanged: it remains the deliberately narrow
demonstration that the fixed L/alpha two-ADD inverse deltas can survive a
process exit at two concrete points.  It is not a general recovery engine; the
general implementation lives in `recovery.py`.
