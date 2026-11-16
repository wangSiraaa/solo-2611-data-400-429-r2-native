# Concurrent page cache and in-flight writeback

    python3 demo.py all
    python3 demo.py <scenario>

Python 3.11 standard library. `fixtures/pages.bin` is eight real 128-byte
pages. PageIO uses `os.pread`/`os.pwrite` on a real file descriptor and has
controlled Event gates plus one-shot read/write failures. Every demo copies the
input to a `TemporaryDirectory`, creates real caller threads, releases gates,
joins all created threads, closes its file, and independently re-reads the
copied bytes. No other database or user file is touched. This is an in-process
page access layer, not a database engine, WAL or crash-recovery system.

## Public surface

`BufferPool(io, capacity)` exposes `fetch(page_id)`, `read(handle, offset,
length=1)`, `write(handle, offset, bytes)`, `release(handle)`, `flush(page_id)`,
`inspect()`, `close()`. `capacity` is 2..4, up to 8 caller threads.

## Loading protocol (pool.py)

The directory holds exactly one *entry* per physical `page_id`. An entry is
`loading` (a reserved slot whose real read has not returned) or `resident`.
All directory transitions happen under one `RLock`/`Condition`; slow I/O is
never executed under it.

* A cold `fetch` publishes a `loading` entry **before** touching disk. A second
  concurrent fetch of the same page sees it, records `load_wait`, and blocks on
  the condition. When the read finishes the single frame is published and both
  callers pin that one frame — one disk read, one visible image. There is no
  window in which two frames are returned and only one survives in the
  directory.
* Capacity counts `loading` reservations plus resident frames, so misses can
  never each observe the same free slot and over-allocate. With every slot
  pinned or reserved, `fetch` raises `busy/all_pinned` (also emitted as a
  `fetch_busy` event) instead of blocking forever.
* If the real read fails, the reservation is removed, the entry becomes
  `failed`, and the condition is broadcast. The loader re-raises the I/O error;
  every waiter observes the failed frame, emits `load_failed_waiter`, and
  raises `OSError("load failed: ...")`. No `loading` state is left behind, so a
  later `fetch` simply reserves a fresh slot and succeeds. `fetch` bounds its
  eviction-retry loop (`MAX_LOAD_RETRIES`) rather than spinning.

A handle pins its frame for its whole lifetime. `release` is idempotent-safe
and refuses invalid handles; a second release or any use after release raises
`ValueError("released handle")`. `read`/`write` are whole operations under the
lock, so a completed write is visible to every later same-page handle and a
reader can never observe a half-applied splice. A write bumps `generation`,
which is the modification generation; `dirty` is derived as
`generation > persisted_generation`.

## Writeback organization (writeback.py)

`flush(page_id)` is the awaitable step of the asynchronous write path. Per page
there is at most one in-flight **ticket**; a ticket has a stable identity, the
immutable captured full-page payload, and the exact captured generation. The
real `pwrite` runs with no pool lock held.

* **Capture.** The payload is copied under the lock, so a pinned writer may
  immediately keep reading/writing the live frame while older bytes are on the
  way to disk. A successful write advances `persisted_generation` **only to the
  captured generation**. If a write landed during the I/O, `generation >
  persisted_generation` still holds, the page stays dirty, and the flush result
  says so (`dirty: true`, `durable_generation` < `current_generation`). An old
  writeback never claims a newer change is durable.
* **Overlap.** A second flush on a page with a ticket in flight joins it
  (recorded as `wb_join`, `joined_ticket`, `joiners`) and waits for its
  completion. If the captured bytes were already durable it then re-checks:
  clean -> returns `clean`; a newer dirty generation -> allocates a *new* ticket
  and writes the newer bytes. Therefore two same-page flushes are serialized
  and an older completion can never overwrite a newer image on disk, whichever
  thread triggered them. The second flush is allowed to finish after the first;
  no throughput ordering is promised.
* **Failure.** A failed `pwrite` marks the ticket `failed`, clears that page's
  in-flight reservation, keeps memory and dirtiness untouched, and returns
  `status: "failed"` with the error. The failure is *not* reported as clean.
  A later flush allocates a fresh ticket and retries.

`inspect()` reports frames (state, pins, generation, persisted generation,
in-flight ticket) plus the writeback ticket table, so task identity, captured
vs current generation, and success/failure are all observable.

## Eviction and pin coordination

`fetch` chooses an unpinned resident victim and then evicts **outside** the
lock: if the victim is dirty it flushes it first; then, atomically, it
re-checks that the frame is still the same page, still resident, still
unpinned, and not writeback-in-flight. If a caller re-pinned the victim while
the flush was running, eviction is abandoned (`evict_abandoned`) and the fetch
retries — a freshly returned handle is never redirected to a repurposed frame.
If the flush failed, the victim cannot be made durable and eviction is
abandoned. Only frames whose latest modifications are truly on disk and that
have no valid handle are reclaimed.

## Close

`close()` refuses while any valid handle exists (`still_pinned`) and refuses
(and leaves the pool usable) if a load is still in flight beyond a bounded
wait. Otherwise it stops accepting new callers, waits for any in-flight
writeback, flushes every dirty resident page, and only then closes the
descriptor. A writeback failure during draining raises
`close_flush_failed: page N ticket T: ...`, leaves the descriptor open, the
dirty state intact, and the pool retryable — it never reports the latest page
as persisted. After a successful retry, `close()` succeeds and closes.

## Scenarios in `demo.py`

`normal` (serial hit/modify/durable), `double-load`, `dirty-during-flush`,
`overlapping-flush`, `unrelated-progress`, plus controlled interleavings:
`concurrent-load` (same-page cold loads share one read and one frame),
`load-failure` (loader and waiter fail clearly; retry succeeds; no stale
LOADING), `write-failure-retry` (failure keeps dirty; retry persists),
`evict-repin` (victim re-pinned during its own flush is not repurposed),
`all-pinned` (explicit `busy/all_pinned`), `capacity-reservation` (LOADING
slots count against capacity), `close-refused`, `close-drains-writeback`, and
`close-write-failure` (failure leaves fd open and retryable, then close
succeeds).

The `.1s` joins only observe whether a waiter is still blocked; correctness is
judged from frame sharing, real I/O counts, captured generations, and the
independently read file bytes. Event waits are deadlock diagnostics, not
timing targets.

## Alternative organization, and its cost

A **future/promise per page** variant (a per-page `Future` stored in a loading
map, completing on load, plus a per-page writeback queue with one worker thread
and generation-tagged completion callbacks) gives the same semantics:
waiters await the future; the write queue serializes tickets and re-checks
dirtiness. Its condition: completion must carry the ticket/generation, and the
eviction path must cancel or revalidate a queued victim, exactly as here.
The cost is a background worker per pool (or queue) to join on close, plus
cancellation bookkeeping for abandoned victims; this design instead uses a
single condition variable and synchronous awaitable flushes, which is simpler
to drain deterministically and avoids a thread to shut down, at the price of
flushes occupying the calling thread while they perform their I/O.
