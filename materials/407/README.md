# Extended-query pipeline subset

    python3 main.py fixtures/normal.json
    python3 main.py fixtures/skip.json
    python3 main.py fixtures/aborted.json
    python3 main.py fixtures/skip.json --sync per-command   # opt-in
    python3 main.py fixtures/skip.json --plan               # grouping only
    python3 verify.py

`SCHEDULE_JSON` may be used in place of the positional schedule argument. Only
Python 3.11 stdlib. backend.py exchanges actual framed bytes on a socket pair
and fragments writes into 3-byte pieces. P payload is statement-name NUL SQL; B
payload is portal NUL statement-name; E payload is portal; S is Sync; X closes.
Backend 1/2 are ParseComplete/BindComplete, T/D are column/row JSON, C command
tag, E error text, Z one-byte transaction state I/T/E. This framing/lifecycle
subset is explicit and not a full PostgreSQL wire implementation. SQLite
executes the fixed small SQL, while the facility models PostgreSQL's
error-until-Sync and failed-transaction behavior. Error skips all later P/B/E
before the next S. Sync ends that skip phase but does not automatically roll
back failed transactions. There is no request id in backend messages.
Application may alter decoder/pipeline, not backend outcomes or wire contract.
Input id is an application identity; it is never echoed by backend packets.

## Attribution model

`decoder.read_cycle` is event-preserving and makes no attribution decisions. It
returns the ordered events seen up to (and including) the next Z, plus the
ReadyForQuery transaction byte and a `termination` value of `ready`, `eof`,
`timeout`, `io_error`, or `protocol_error`. `ParseComplete`/`BindComplete` are
acknowledgements, not completion; `CommandComplete` and `Error` are the two
distinct terminal events.

`pipeline.attribute_group` attributes one Sync group's events to that group's
commands in send order using the per-command grammar:

    P -> 1
    B -> 2
    E -> (T D*)? (C | E)

Rules enforced:

* Attribution never crosses a Sync boundary, so a later cycle's success cannot
  fill a skipped command from an earlier cycle.
* A `C` proves the command itself succeeded and carries its own columns, rows,
  and tag. An `E` is a failure; every later command in the same group is
  `not_executed` (`blocked_by` names the failing id), because the server
  ignores P/B/E until Sync.
* Skipped commands are never reported as success or silently dropped. Every
  input id gets exactly one record.
* If the stream ends before ReadyForQuery (EOF, half frame, timeout, length
  error), undecided commands are `incomplete`, not success and not
  `not_executed`. A timeout proves only that no complete evidence arrived; it
  does not prove execution or skipping. Events already decoded stay attributed
  to their own commands. The pipeline stops after such a failure and marks
  later unsent commands `not_executed` with a `not sent` reason (distinct from
  the server-skip reason).

`aborted.json` shows the transaction distinction: after `BEGIN` and a failing
statement, Sync returns Z with `E`, and the next cycle's business query is a
real `25P02` error, not a skip and not a success. Only the explicit `ROLLBACK`
returns Z to `I`; `read` then succeeds with the original value.

Each result has `status` in `success` / `error` / `not_executed` /
`incomplete`. Cycles record `send_order`, `receive_order`, per-group events,
and the actual ReadyForQuery bytes in `ready_states` / `transaction`; top-level
`send_log`/`receive_log` give global order.

`backend_audit` is used only by `verify.py` for independent cross-checking; the
production attribution reads only received packets. `verify.py` also checks
the final read values (the skipped `UPDATE` leaves the counter at 10, while
per-command Sync lets it run and the value becomes 110).

## Sync grouping

The default plan is exactly the input cycles: P/B/E for every command in a
cycle, then one S. `--sync per-command` sends P/B/E/S per command. The final
grouping is computed before the socket is created and is returned as
`sync_plan`; `run(..., plan_hook=...)`/`--plan` expose it before sending. The
default grouping is not silently changed.

Comparison:

| | batch (one Sync per cycle) | per-command Sync |
|---|---|---|
| Error isolation | An error skips all later P/B/E in the cycle; they are `not_executed` | An error skips nothing; the next command is parsed/bound/executed in a fresh group |
| Error scope | One failure suppresses the rest of the cycle's batch | Failure affects only its own command |
| Round trips / Syncs | One S/Z per cycle | One S/Z per command |
| Wait / latency | One wait per cycle after the whole batch | One wait per command, serializing round trips |
| Skipped work | Later commands in the cycle | None (aside from the failed statement) |
| Suits | Throughput when commands are independent and failures are rare | Diagnosing/attributing individual commands, or a cycle that must not lose later commands |

Because Sync only ends the skip phase and does not roll back a failed
transaction, neither mode recovers an aborted transaction automatically; an
explicit ROLLBACK is still required.
