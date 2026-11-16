"""Versioned, single-flight writeback tickets.

A flush call is the awaitable step of the asynchronous write path.  For a
page, at most one *writeback ticket* is in flight: a ticket carries a stable
identity, the immutable full-page payload and the exact generation that was
captured.  Real ``os.pwrite`` runs with no pool lock held, so a thread that
still pins the page can keep reading/writing it while the older bytes are on
their way to disk.

Completion semantics
--------------------
* Success advances ``frame.persisted_generation`` **to the captured
  generation only**.  If shadow writes happened during the I/O, the frame
  stays dirty (``generation > persisted_generation``); an old writeback never
  claims a newer modification is durable.
* Because a page has one in-flight ticket, two flushes on the same page are
  serialized by joining that ticket and then re-checking dirtiness.  A newer
  payload therefore can never be overwritten by an older completion, no matter
  which thread triggered it.
* Failure marks the ticket failed, clears the in-flight reservation, leaves
  memory and dirtiness untouched, and reports the error.  A later flush may
  allocate a new ticket and retry.

Result fields
-------------
``status``       absent | loading | clean | written | joined_clean | failed
``written``      True iff this call itself performed the durable write
``ticket``       identity of the ticket this call drove (if any)
``joined_ticket``identity of an older ticket this call waited on (if any)
``captured_generation`` generation whose bytes were written by this call
``durable_generation``  frame.persisted_generation when returning
``error``        diagnostic string for a failed ticket
"""
from dataclasses import dataclass, field
import threading


@dataclass(eq=False)
class Ticket:
    identity: int
    page_id: int
    generation: int
    frame_identity: int
    state: str = "pending"          # pending -> running -> done | failed
    error: str = None
    driver: str = None
    joiners: int = 0
    events: list = field(default_factory=list)


class Writeback:
    def __init__(self, pool):
        self.pool = pool
        self.tickets = {}
        self.next_identity = 1
        self.draining = False

    # ------------------------------------------------------------ lifecycle
    def begin_drain(self):
        self.draining = True

    def end_drain(self):
        self.draining = False

    def wait_idle(self):
        pool = self.pool
        with pool.cond:
            while any(f.wb_inflight for f in pool.frames.values()):
                pool.cond.wait()

    def inspect(self):
        pool = self.pool
        with pool.lock:
            return {"next_ticket": self.next_identity,
                    "inflight": [{"page_id": f.page_id, "frame": f.identity,
                                  "ticket": f.wb_ticket, "generation": f.wb_generation}
                                 for f in pool.frames.values() if f.wb_inflight],
                    "tickets": [{"ticket": t.identity, "page_id": t.page_id,
                                 "generation": t.generation,
                                 "frame": t.frame_identity, "state": t.state,
                                 "error": t.error, "driver": t.driver,
                                 "joiners": t.joiners}
                                for t in self.tickets.values()]}

    # ---------------------------------------------------------------- flush
    def flush(self, page_id):
        pool = self.pool
        joined = None
        while True:
            with pool.cond:
                frame = pool.frames.get(page_id)
                if frame is None:
                    return self._result(page_id, "absent", joined=joined)
                if frame.state == "loading":
                    return self._result(page_id, "loading", joined=joined)
                if frame.wb_inflight:
                    ticket = self.tickets[frame.wb_ticket]
                    ticket.joiners += 1
                    joined = ticket.identity
                    ticket.events.append("join:%s" % threading.current_thread().name)
                    pool.events.append({"event": "wb_join", "page_id": page_id,
                                        "ticket": ticket.identity,
                                        "generation": ticket.generation,
                                        "thread": threading.current_thread().name})
                    while ticket.state in ("pending", "running"):
                        pool.cond.wait()
                    if ticket.state == "failed":
                        return self._result(page_id, "failed", joined=joined,
                                            ticket=ticket.identity,
                                            captured=ticket.generation,
                                            error=ticket.error)
                    # Old bytes are durable; loop to check for a newer dirty
                    # generation and, if present, write it with a new ticket.
                    continue
                if not frame.dirty:
                    return self._result(page_id, "clean", joined=joined,
                                        durable=frame.persisted_generation)
                ticket = Ticket(self.next_identity, page_id, frame.generation,
                                frame.identity)
                ticket.driver = threading.current_thread().name
                ticket.state = "running"
                self.next_identity += 1
                self.tickets[ticket.identity] = ticket
                payload = bytes(frame.data)
                generation = frame.generation
                frame.wb_inflight = True
                frame.wb_generation = generation
                frame.wb_ticket = ticket.identity
                frame.wb_failure = None
                frame.wb_attempts += 1
                ticket.events.append("start:%s" % ticket.driver)
                pool.events.append({"event": "wb_start", "page_id": page_id,
                                    "frame": frame.identity,
                                    "ticket": ticket.identity,
                                    "generation": generation,
                                    "persisted_generation": frame.persisted_generation,
                                    "thread": ticket.driver})
                break

        # ---- real I/O, deliberately without the pool lock ----
        error = None
        try:
            pool.io.write_page(page_id, payload)
        except BaseException as exc:
            error = str(exc)
        with pool.cond:
            if error is None:
                ticket.state = "done"
                ticket.events.append("done")
                pool.complete_write(frame, ticket.identity, generation)
                return self._result(page_id, "written", joined=joined,
                                    ticket=ticket.identity,
                                    captured=generation,
                                    durable=generation)
            ticket.state = "failed"
            ticket.error = error
            ticket.events.append("failed:%s" % error)
            pool.fail_write(frame, ticket.identity, error)
            return self._result(page_id, "failed", joined=joined,
                                ticket=ticket.identity, captured=generation,
                                error=error)

    # ---------------------------------------------------------------- helper
    def _result(self, page_id, status, joined=None, ticket=None, captured=None,
                durable=None, error=None):
        pool = self.pool
        with pool.lock:
            frame = pool.frames.get(page_id)
            if durable is None:
                durable = frame.persisted_generation if frame else None
            dirty = frame.dirty if frame else None
            current = frame.generation if frame else None
        return {"page_id": page_id, "status": status,
                "written": status == "written",
                "ticket": ticket, "joined_ticket": joined,
                "captured_generation": captured,
                "durable_generation": durable,
                "dirty": dirty, "current_generation": current,
                "error": error}
