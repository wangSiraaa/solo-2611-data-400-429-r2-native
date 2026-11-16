# Concurrent page cache and in-flight writeback

    python3 demo.py all

Python 3.11 standard library. fixtures/pages.bin is eight real 128-byte pages. PageIO uses os.pread/os.pwrite on a real file descriptor and has controlled Event gates plus one-shot read/write failures. Every demo copies the input to a TemporaryDirectory, creates real caller threads, releases gates, joins all created threads, and closes its file. No other database or user file is touched. This is an in-process page access layer, not an actual database engine, WAL or crash recovery system.

The ordinary serial route is operational: repeated fetch shares one frame, a handle's modification is visible through another handle, and flush writes real disk bytes. The prototype releases its metadata lock around slow I/O, but does not reserve a loading page; two concurrent same-page fetches can create different frames. It also clears dirty after any old writeback, even if a pinned writer changed the shared frame during that write. The demo records actual page-I/O events and reads the copied file independently.

The .1-second join in double-load only observes the current baseline's early second return; correctness is judged from one logical frame / one disk load and shared bytes after both callers finish. It is not a performance requirement. Event waits are bounded deadlock diagnostics, not wall-clock speed targets. Unrelated-progress requires a causal event while a selected I/O gate remains blocked, because holding a global lock across I/O defeats independent page access. Per-page locks, loading conditions, futures, a writeback queue or another correct lifecycle organization are all allowed.
