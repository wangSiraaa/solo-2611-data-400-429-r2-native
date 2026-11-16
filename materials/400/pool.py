"""Concurrent page pool: one owner per page, loading reservations, versioned writeback.

Directory protocol (all under ``self.lock``; slow I/O is never under it):

* ``frames[page_id]`` is the single entry that owns a physical page.  Its
  ``state`` is LOADING from the moment a slot is reserved until the real read
  completes, then RESIDENT.  Concurrent cold fetches wait on the same entry and
  pin the single successful frame.
* Capacity counts LOADING reservations plus RESIDENT frames, so two misses
  cannot both observe the same free slot.
* A write is a whole page-internal operation under the lock: it bumps
  ``generation`` and makes ``generation > persisted_generation`` (dirty).
* A flush is implemented by ``writeback.Writeback``: immutable bytes are
  captured under the lock, real I/O runs outside it, and completion advances
  ``persisted_generation`` only to the captured generation.
"""
from dataclasses import dataclass
from typing import Optional
import threading
import time

LOADING = "loading"
RESIDENT = "resident"
FAILED = "failed"
EVICTED = "evicted"


@dataclass(eq=False)
class Frame:
    identity: int
    page_id: int
    data: Optional[bytearray] = None
    pins: int = 0
    generation: int = 0
    persisted_generation: int = 0
    state: str = LOADING
    wb_inflight: bool = False
    wb_generation: int = 0
    wb_ticket: Optional[int] = None
    wb_failure: Optional[str] = None
    wb_attempts: int = 0
    error: Optional[str] = None

    @property
    def dirty(self):
        return self.generation > self.persisted_generation


@dataclass(eq=False)
class Handle:
    frame: Frame
    active: bool = True


class BufferPool:
    MAX_LOAD_RETRIES = 8

    def __init__(self, io, capacity):
        self.io = io
        self.capacity = capacity
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.frames = {}          # page_id -> Frame (loading or resident)
        self.created = []         # every frame ever allocated (for close/pins)
        self.events = []
        self.closed = False
        from writeback import Writeback
        self.writeback = Writeback(self)

    # ------------------------------------------------------------------ fetch
    def fetch(self, page_id):
        if not 0 <= page_id < self.io.page_count:
            raise ValueError("page id out of range")
        retries = 0
        while True:
            action = None
            with self.cond:
                if retries > self.MAX_LOAD_RETRIES:
                    self.events.append({"event": "load_retry_exhausted",
                                        "page_id": page_id})
                    raise RuntimeError("load_retry_exhausted")
                if self.closed:
                    raise RuntimeError("pool_closed")
                entry = self.frames.get(page_id)
                if entry is not None:
                    if entry.state == LOADING:
                        self.events.append({"event": "load_wait", "page_id": page_id,
                                            "frame": entry.identity,
                                            "thread": threading.current_thread().name})
                        waiting_frame = entry
                        self.cond.wait()
                        if waiting_frame.state == FAILED:
                            self.events.append({"event": "load_failed_waiter",
                                                "page_id": page_id,
                                                "frame": waiting_frame.identity,
                                                "error": waiting_frame.error,
                                                "thread": threading.current_thread().name})
                            raise OSError("load failed: %s" % waiting_frame.error)
                        continue
                    # RESIDENT: hand out a pin on the one owning frame.
                    entry.pins += 1
                    self.events.append({"event": "hit", "page_id": page_id,
                                        "frame": entry.identity, "pins": entry.pins,
                                        "thread": threading.current_thread().name})
                    return Handle(entry)

                if len(self.frames) >= self.capacity:
                    victim = next((f for f in self.frames.values()
                                   if f.state == RESIDENT and f.pins == 0), None)
                    if victim is None:
                        self.events.append({"event": "fetch_busy", "page_id": page_id})
                        raise RuntimeError("busy/all_pinned")
                    action = ("evict", victim)
                else:
                    frame = Frame(len(self.created), page_id)
                    frame.state = LOADING
                    self.frames[page_id] = frame
                    self.created.append(frame)
                    self.events.append({"event": "loading", "page_id": page_id,
                                        "frame": frame.identity,
                                        "thread": threading.current_thread().name})
                    action = ("load", frame)
            if action[0] == "evict":
                if not self._evict_victim(action[1]):
                    retries += 1
                    with self.lock:
                        self.events.append({"event": "evict_abandoned",
                                            "page_id": action[1].page_id,
                                            "retry": retries})
                    continue
                continue
            return self._perform_load(action[1])

    def _perform_load(self, frame):
        page_id = frame.page_id
        try:
            data = self.io.read_page(page_id)
        except BaseException as exc:
            with self.cond:
                if self.frames.get(page_id) is frame:
                    del self.frames[page_id]
                frame.state = FAILED
                frame.error = str(exc)
                self.events.append({"event": "load_failed", "page_id": page_id,
                                    "frame": frame.identity, "error": str(exc),
                                    "thread": threading.current_thread().name})
                self.cond.notify_all()
            raise
        with self.cond:
            if self.frames.get(page_id) is not frame:
                # Our slot was reclaimed (e.g. close); report a definite failure.
                frame.state = FAILED
                frame.error = "load slot reclaimed"
                self.cond.notify_all()
                raise RuntimeError("pool_closed")
            frame.data = bytearray(data)
            frame.state = RESIDENT
            frame.pins = 1
            self.events.append({"event": "loaded", "page_id": page_id,
                                "frame": frame.identity, "pins": 1,
                                "thread": threading.current_thread().name})
            self.cond.notify_all()
            return Handle(frame)

    # -------------------------------------------------------------- eviction
    def _evict_victim(self, frame):
        page_id = frame.page_id
        attempts = 0
        while True:
            need_flush = False
            with self.cond:
                if (self.frames.get(page_id) is not frame
                        or frame.state != RESIDENT or frame.pins > 0):
                    return False           # re-pinned or already gone: abandon
                if frame.wb_inflight:
                    self.cond.wait(0.25)   # cooperate with in-flight writeback
                    continue
                if frame.dirty:
                    need_flush = True
                else:
                    del self.frames[page_id]
                    frame.state = EVICTED
                    self.events.append({"event": "evicted", "page_id": page_id,
                                        "frame": frame.identity})
                    self.cond.notify_all()
                    return True
            if need_flush:
                result = self.flush(page_id)   # outside the lock
                if result["status"] == "failed":
                    return False               # cannot make victim durable
                attempts += 1
                if attempts > 2 + self.MAX_LOAD_RETRIES:
                    return False
                # loop re-checks pins and dirtiness after the writeback

    # ----------------------------------------------------------- page access
    def _check(self, handle, offset, length):
        if not isinstance(handle, Handle) or not handle.active:
            raise ValueError("released handle")
        if not 0 <= offset <= offset + length <= self.io.page_size:
            raise ValueError("page bounds")

    def read(self, handle, offset=0, length=1):
        with self.lock:
            self._check(handle, offset, length)
            frame = handle.frame
            if frame.state != RESIDENT:
                raise ValueError("frame not resident")
            return bytes(frame.data[offset:offset + length])

    def write(self, handle, offset, data):
        data = bytes(data)
        with self.lock:
            self._check(handle, offset, len(data))
            frame = handle.frame
            if frame.state != RESIDENT:
                raise ValueError("frame not resident")
            # Whole-page atomic mutation: readers see before or after, never mix.
            frame.data[offset:offset + len(data)] = data
            frame.generation += 1
            self.events.append({"event": "modified", "page_id": frame.page_id,
                                "frame": frame.identity,
                                "generation": frame.generation,
                                "persisted_generation": frame.persisted_generation,
                                "thread": threading.current_thread().name})
            self.cond.notify_all()
            return frame.generation

    def release(self, handle):
        with self.cond:
            self._check(handle, 0, 0)
            handle.active = False
            handle.frame.pins -= 1
            self.events.append({"event": "released", "page_id": handle.frame.page_id,
                                "frame": handle.frame.identity,
                                "pins": handle.frame.pins,
                                "thread": threading.current_thread().name})
            self.cond.notify_all()

    # ------------------------------------------------------------ writeback
    def flush(self, page_id):
        return self.writeback.flush(page_id)

    def complete_write(self, frame, ticket, generation):
        """Durably persisted ``generation``; only that generation becomes clean."""
        with self.cond:
            if self.frames.get(frame.page_id) is frame:
                if generation > frame.persisted_generation:
                    frame.persisted_generation = generation
                frame.wb_inflight = False
                frame.wb_ticket = None
                frame.wb_failure = None
                self.events.append({"event": "wb_complete",
                                    "page_id": frame.page_id,
                                    "frame": frame.identity, "ticket": ticket,
                                    "persisted_generation": generation,
                                    "current_generation": frame.generation,
                                    "dirty": frame.dirty,
                                    "thread": threading.current_thread().name})
            self.cond.notify_all()

    def fail_write(self, frame, ticket, error):
        """Write failed: keep memory and dirtiness; release our reservation."""
        with self.cond:
            if self.frames.get(frame.page_id) is frame:
                frame.wb_inflight = False
                frame.wb_ticket = None
                frame.wb_failure = str(error)
                self.events.append({"event": "wb_failed",
                                    "page_id": frame.page_id,
                                    "frame": frame.identity, "ticket": ticket,
                                    "current_generation": frame.generation,
                                    "persisted_generation": frame.persisted_generation,
                                    "dirty": frame.dirty, "error": str(error),
                                    "thread": threading.current_thread().name})
            self.cond.notify_all()

    # ------------------------------------------------------------- lifecycle
    def inspect(self):
        with self.lock:
            return {
                "capacity": self.capacity,
                "frames": [{"page_id": p, "frame": f.identity, "state": f.state,
                            "pins": f.pins, "dirty": f.dirty,
                            "generation": f.generation,
                            "persisted_generation": f.persisted_generation,
                            "wb_inflight": f.wb_inflight,
                            "wb_ticket": f.wb_ticket,
                            "wb_generation": f.wb_generation,
                            "wb_failure": f.wb_failure}
                           for p, f in sorted(self.frames.items())],
                "created_frames": len(self.created),
                "events": list(self.events),
                "writeback": self.writeback.inspect(),
            }

    def close(self):
        with self.cond:
            if self.closed:
                return
            if any(f.pins for f in self.created):
                self.events.append({"event": "close_refused", "reason": "still_pinned"})
                raise RuntimeError("still_pinned")
            self.writeback.begin_drain()
            self.closed = True
            # A cold fetch may have already reserved a LOADING slot (pins == 0).
            # Let it resolve before deciding whether close can proceed; bound
            # the wait so a stuck loader cannot hang close forever.
            deadline = time.monotonic() + 3.0
            while any(f.state == LOADING for f in self.frames.values()):
                remaining_t = deadline - time.monotonic()
                if remaining_t <= 0:
                    self.closed = False
                    self.writeback.end_drain()
                    self.events.append({"event": "close_refused",
                                        "reason": "load_in_flight"})
                    raise RuntimeError("load_in_flight")
                self.cond.wait(remaining_t)
            if any(f.pins for f in self.created):
                self.closed = False
                self.writeback.end_drain()
                self.events.append({"event": "close_refused",
                                    "reason": "load_became_pinned"})
                raise RuntimeError("still_pinned")
        try:
            # Drain anything already in flight, then flush every dirty page.
            self.writeback.wait_idle()
            while True:
                with self.cond:
                    dirty = [p for p, f in self.frames.items()
                             if f.state == RESIDENT and f.dirty]
                if not dirty:
                    break
                for page in dirty:
                    result = self.flush(page)
                    if result["status"] == "failed":
                        raise RuntimeError(
                            "close_flush_failed: page %d ticket %s: %s"
                            % (page, result.get("ticket"), result.get("error")))
            self.writeback.wait_idle()
            with self.cond:
                remaining = [p for p, f in self.frames.items()
                             if f.state == RESIDENT and f.dirty]
            if remaining:
                raise RuntimeError("close_flush_failed:%s" % sorted(remaining))
        except BaseException:
            # Leave the descriptor open and the pool retryable: a failed
            # writeback must not be reported as a successful durable close.
            with self.cond:
                self.closed = False
            self.writeback.end_drain()
            raise
        else:
            self.io.close()
