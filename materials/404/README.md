# Incremental GraphQL relation preview

Python 3.11 standard library only.

```sh
python3 main.py fixtures/single.json 1
python3 main.py fixtures/null-bubble.json 3
python3 main.py fixtures/deferred.json 2 /tmp/preview.sqlite
python3 batch.py fixtures/null-bubble.json /tmp/batch.sqlite
python3 tests.py
python3 publication_demo.py
python3 analysis.py fixtures/null-bubble.json
```

`FrameSource.read()` returns one already-decoded envelope and nothing more; it
does not assemble a document or decide completion. The session owns the small
dialect documented in `protocol.json`: the first envelope carries `data`, later
envelopes carry `incremental` entries; object `data` merges, `data: null`
replaces an exact subtree, and `items` inserts at the final integer list
position. `hasNext` is a Boolean. There is no HTTP/MIME parsing, no general
GraphQL execution and no support for other incremental drafts.

## Session

`Session(path, db_path=None)` pumps a fixture. `pump()` consumes **at most one**
envelope and publishes the resulting preview to SQLite; `query()` reads the
published preview through a fresh independent connection, so it is always an
observable intermediate data product between pumps, not a hidden in-memory
value. `Session(path)` uses a temporary database; `Session(path, db_path)`
publishes to the given database. `db_path` is exposed as `session.db_path`, so
`sql_consumer.snapshot(session.db_path)` reads the same preview.

Statuses:

| status | meaning |
| --- | --- |
| `receiving` | a legal non-final frame (`hasNext=true`) was published |
| `complete` | a legal final frame without GraphQL errors |
| `complete_with_errors` | legal final frame carrying GraphQL errors (patches applied) |
| `incomplete` | EOF while `hasNext` was still true; last preview retained |
| `failed` | protocol error, duplicate business identity or publish failure |

A first frame with `hasNext=true` is immediately visible with status
`receiving`; the session does not hide it until the last frame. A `data: null`
patch removes that subtree and every relation beneath it, while a `null` list
element keeps its index so later paths do not shift. Object patches merge into
the existing object, so a deferred `tax` does not create a second order.

## Provenance

Each relation row carries `source_path` (a compact JSON-array string),
`created_frame` (the frame in which the current source occurrence appeared) and
`last_changed_frame` (only moves when the row's value or source path changes).
Array insertion shifts source paths; the shifted rows change
`last_changed_frame` even when their values are unchanged. Rows rebuilt
unchanged by a full materialization keep their old frame numbers.

## Atomic SQLite publication

`preview_meta`, `orders` and `lines` are written in one `BEGIN IMMEDIATE`
transaction in WAL mode. An independent reader never mixes frames and, while a
new frame is being prepared, still sees the previous committed frame.
`session.publish_hook` runs inside that transaction immediately before commit:
it can pause (external readers see the previous frame) or raise `OSError`
(the whole frame rolls back). The old single-response reader and append-only
materializer remain in `reader.py` and `materializer.py` for comparison; they
only work on a single complete response.

`publication_demo.py` uses real independent SQLite connections to show the
normal path, a paused publication, an injected failure and the resulting
terminal session state.

## Cost of an ancestor invalidation

`analysis.py` reports, per frame, visited relation `nodes`, rows
added/changed/removed/unchanged, the logical `minimal_writes` an incremental
path-dependency index would perform, and `sql_rows_written` (the full snapshot
rewritten by this implementation). Nulling an ancestor therefore shows both
the invalidation walk and the cost an incrementally maintained index would
save; both approaches are acceptable because the contract is the current
preview and the explicit terminal state.
