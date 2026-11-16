# Incremental GraphQL relation preview

    python3 main.py fixtures/single.json 1
    python3 main.py fixtures/null-bubble.json 3

Python 3.11 stdlib only. FrameSource provides decoded envelopes for the limited protocol in protocol.json. It does not assemble the result or decide success. This is the fixture's explicit incremental-delivery dialect; no general GraphQL server, schema compiler, MIME parser or claim of supporting every draft is required. Each pump consumes at most one frame, so query() between pumps is an observable data-product preview. A hasNext=true frame is not final; EOF while that condition remains true is incomplete. Query rows are provisional until the documented terminal state. The existing single-envelope reader and append-only materializer work on single.json. Applications must read through FrameSource.read, not inspect _frames. The separate append-only probe shows why forwarding every patch alone is insufficient when an error sets an ancestor to null.

The analysis dashboard uses SQLite through `sql_consumer.snapshot(db_path)` on a
separate connection. `Session(path, db_path)` publishes a preview database; the
one-argument constructor remains available with a temporary database. The given
`SQLiteView` is application code and deliberately commits rows and metadata in
separate transactions. A single ordinary response works, but publication is not
atomic. `session.publish_hook` is an optional no-argument callback for a test to
pause immediately before publishing a frame or inject an `OSError`. Readers must
still see the previous published frame while that callback is active. The
callback does not request concurrent writers or crash recovery. An independent
consumer may keep a read transaction open; it must see one whole snapshot.

`publication_demo.py` demonstrates the normal path and two publication defects
using actual independent SQLite connections. It does not implement the repair.
