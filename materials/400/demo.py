"""Controlled interleavings over real files, with real caller threads.

    python3 demo.py all
    python3 demo.py double-load

Every scenario copies fixtures/pages.bin into a TemporaryDirectory, drives
real ``os.pread``/``os.pwrite`` through PageIO gates/Barriers, then reads the
copied file directly to verify durable bytes.  All created threads are joined
and all gates released in ``finally``; correctness never depends on a short
join (the .1s waits only observe whether a waiter is still blocked).
"""
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from page_io import PageIO
from pool import BufferPool

FIXTURE = Path(__file__).parent / "fixtures" / "pages.bin"


def worker(name, fn):
    outcome = {}

    def run():
        try:
            outcome["value"] = fn()
        except BaseException as exc:            # keep the failure observable
            outcome["error"] = "%s: %s" % (type(exc).__name__, exc)
    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread, outcome


def wait(event, what="controlled event"):
    if not event.wait(3):
        raise RuntimeError("controlled schedule did not reach %s" % what)


def join(thread, what=None):
    thread.join(3)
    if thread.is_alive():
        raise RuntimeError("worker %s remains blocked after gate release"
                           % (what or thread.name))


def disk_byte(path, page):
    with open(path, "rb") as f:
        f.seek(page * 128)
        return f.read(1).hex()


def disk_pages(path):
    with open(path, "rb") as f:
        data = f.read()
    return [data[i * 128:(i + 1) * 128][:1].hex()
            for i in range(len(data) // 128)]


def unchanged_rest(path):
    return disk_pages(path)[1:]


SCENARIOS = ["normal", "double-load", "dirty-during-flush", "overlapping-flush",
             "unrelated-progress", "concurrent-load", "load-failure",
             "write-failure-retry", "evict-repin", "all-pinned",
             "close-refused", "close-drains-writeback", "close-write-failure",
             "capacity-reservation"]


def scenario(name):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "pages.bin"
        shutil.copyfile(FIXTURE, path)
        io = PageIO(path)
        pool = BufferPool(io, 3 if name == "unrelated-progress" else 2)
        handles = []
        threads = []
        gates = []
        close_attempt = {}

        def track(handle):
            handles.append(handle)
            return handle

        def retire(handle):
            if handle.active:
                pool.release(handle)

        try:
            if name == "normal":
                a = track(pool.fetch(0))
                b = track(pool.fetch(0))
                pool.write(a, 0, b"X")
                before = pool.read(b).hex()
                flushed = pool.flush(0)
                result = {"same_frame": a.frame is b.frame,
                          "second_handle": before,
                          "flush": flushed,
                          "disk": disk_byte(path, 0)}

            elif name == "double-load":
                # Baseline-counterexample: previously two frames and 41 vs 59.
                gate = io.pause_next("read", 0)
                gates.append(gate)
                first, fo = worker("first-fetch", lambda: pool.fetch(0))
                threads.append(first)
                wait(gate.entered, "first read gate")
                second, so = worker("second-fetch", lambda: pool.fetch(0))
                threads.append(second)
                second.join(.1)
                second_returned_while_first_read_blocked = not second.is_alive()
                if "value" in so:
                    pool.write(so["value"], 0, b"Y")
                gate.release()
                join(first)
                join(second)
                a = fo.get("value")
                b = so.get("value")
                if a is not None:
                    track(a)
                if b is not None:
                    track(b)
                c = track(pool.fetch(0))
                reads = [e for e in io.inspect()
                         if e["event"] == "begin" and e["operation"] == "read"]
                result = {"second_returned_while_first_read_blocked":
                          second_returned_while_first_read_blocked,
                          "first": None if a is None else pool.read(a).hex(),
                          "second": None if b is None else pool.read(b).hex(),
                          "new_fetch": pool.read(c).hex(),
                          "same_frame": (a is not None and b is not None
                                         and a.frame is b.frame is c.frame),
                          "disk_reads_for_page_0": len(reads),
                          "created_frames": pool.inspect()["created_frames"]}

            elif name == "concurrent-load":
                # Two cold fetches of page 3 share ONE real disk read.
                gate = io.pause_next("read", 3)
                gates.append(gate)
                first, fo = worker("cold-fetch-a", lambda: pool.fetch(3))
                threads.append(first)
                wait(gate.entered, "page-3 read gate")
                second, so = worker("cold-fetch-b", lambda: pool.fetch(3))
                threads.append(second)
                second.join(.1)
                second_returned_early = not second.is_alive()
                gate.release()
                join(first)
                join(second)
                a, b = fo.get("value"), so.get("value")
                if a is not None:
                    track(a)
                if b is not None:
                    track(b)
                reads = [e for e in io.inspect()
                         if e["event"] == "begin" and e["operation"] == "read"
                         and e["page_id"] == 3]
                result = {"second_returned_before_load_finished":
                          second_returned_early,
                          "same_frame": a is not None and b is not None
                          and a.frame is b.frame,
                          "a": None if a is None else pool.read(a).hex(),
                          "b": None if b is None else pool.read(b).hex(),
                          "disk_reads_for_page_3": len(reads),
                          "created_frames": pool.inspect()["created_frames"],
                          "waiters": [e for e in pool.inspect()["events"]
                                      if e["event"] == "load_wait"]}

            elif name == "load-failure":
                # Injected read failure: loader and waiter both fail clearly,
                # no LOADING entry is left behind, and a later fetch works.
                gate = io.pause_next("read", 4)
                gates.append(gate)
                io.fail_next("read", 4)
                first, fo = worker("failing-load", lambda: pool.fetch(4))
                threads.append(first)
                wait(gate.entered, "failing read gate")
                second, so = worker("failing-waiter", lambda: pool.fetch(4))
                threads.append(second)
                gate.release()
                join(first)
                join(second)
                after_failure = pool.inspect()
                frames_after_failure = [f for f in after_failure["frames"]
                                        if f["page_id"] == 4]
                retry = track(pool.fetch(4))
                result = {"loader_error": fo.get("error"),
                          "waiter_error": so.get("error"),
                          "frames_after_failure": frames_after_failure,
                          "retry_value": pool.read(retry).hex(),
                          "created_frames_after_retry":
                          pool.inspect()["created_frames"],
                          "disk_reads_after_retry":
                          [e for e in io.inspect() if e["event"] == "begin"
                           and e["operation"] == "read" and e["page_id"] == 4]}

            elif name == "dirty-during-flush":
                # Counterexample: old writeback clears a newer dirty flag and
                # claims the newest generation is durable.
                a = track(pool.fetch(0))
                pool.write(a, 0, b"X")
                gate = io.pause_next("write", 0)
                gates.append(gate)
                flush, fo = worker("flush-captured-generation",
                                   lambda: pool.flush(0))
                threads.append(flush)
                wait(gate.entered, "old write gate")
                pool.write(a, 0, b"Y")
                gate.release()
                join(flush)
                after_first = pool.inspect()
                second = pool.flush(0)
                result = {"first_flush": fo,
                          "after_first": after_first,
                          "second_flush": second,
                          "memory": pool.read(a).hex(),
                          "disk": disk_byte(path, 0)}

            elif name == "overlapping-flush":
                # Older 58 must never overwrite newer 59 on disk.
                a = track(pool.fetch(0))
                pool.write(a, 0, b"X")
                gate = io.pause_next("write", 0)
                gates.append(gate)
                first, fo = worker("older-flush", lambda: pool.flush(0))
                threads.append(first)
                wait(gate.entered, "older write gate")
                pool.write(a, 0, b"Y")
                second, so = worker("newer-flush", lambda: pool.flush(0))
                threads.append(second)
                second.join(.1)
                second_returned_early = not second.is_alive()
                gate.release()
                join(first)
                join(second)
                result = {"second_returned_before_older_io":
                          second_returned_early,
                          "first": fo,
                          "second": so,
                          "memory": pool.read(a).hex(),
                          "disk": disk_byte(path, 0),
                          "other_pages_intact": unchanged_rest(path)}

            elif name == "unrelated-progress":
                # A resident unrelated page stays fully usable while one
                # page's writeback is blocked in real IO.
                a = track(pool.fetch(0))
                b = track(pool.fetch(1))
                pool.write(a, 0, b"X")
                gate = io.pause_next("write", 0)
                gates.append(gate)
                flush, fo = worker("paused-flush", lambda: pool.flush(0))
                threads.append(flush)
                wait(gate.entered, "paused write gate")
                progress = threading.Event()
                observed = {}

                def other():
                    # Same already-pinned page 1: write must complete although
                    # page 0's flush IO is still gated.
                    pool.write(b, 0, b"Z")
                    observed["value"] = pool.read(b).hex()
                    progress.set()

                other_thread, oo = worker("page1-write-during-io", other)
                threads.append(other_thread)
                completed = progress.wait(3)
                # Also a cold, unrelated page can be loaded while page-0 IO
                # is blocked (capacity allows a second slot).
                cold = track(pool.fetch(2))
                observed["cold"] = pool.read(cold).hex()
                gate.release()
                join(flush)
                join(other_thread)
                result = {"write_completed_while_io_blocked": completed,
                          "other": observed,
                          "cold_fetch_value": observed.get("cold"),
                          "flush": fo,
                          "disk": disk_byte(path, 0)}

            elif name == "write-failure-retry":
                a = track(pool.fetch(0))
                pool.write(a, 0, b"X")
                io.fail_next("write", 0)
                failed = pool.flush(0)
                state_after_failure = pool.inspect()["frames"]
                disk_after_failure = disk_byte(path, 0)
                retried = pool.flush(0)
                result = {"failed_flush": failed,
                          "frames_after_failure": state_after_failure,
                          "disk_after_failure": disk_after_failure,
                          "retried_flush": retried,
                          "memory": pool.read(a).hex(),
                          "disk": disk_byte(path, 0),
                          "writeback_tickets":
                          pool.inspect()["writeback"]["tickets"]}

            elif name == "evict-repin":
                # A selected victim (dirty page 1) is flushed before eviction;
                # while that IO is blocked the page is pinned again, so the
                # eviction must abandon rather than repurpose its frame.
                a = track(pool.fetch(0))
                b = track(pool.fetch(1))
                pool.write(b, 0, b"Q")
                pool.release(b)
                b.active = False
                gate = io.pause_next("write", 1)
                gates.append(gate)
                evictor, eo = worker("fetch-2-evicting-1", lambda: pool.fetch(2))
                threads.append(evictor)
                wait(gate.entered, "victim write gate")
                repin = track(pool.fetch(1))          # pin victim again
                repin_value = pool.read(repin).hex()
                gate.release()
                join(evictor)
                joined_evictor = eo
                # Snapshot while the re-pin is still held: the victim must
                # still be RESIDENT page 1 on its original frame, never
                # repurposed as page 2.
                snapshot = pool.inspect()["frames"]
                page1_frames = [f for f in snapshot if f["page_id"] == 1]
                page2_present = any(f["page_id"] == 2 for f in snapshot)
                # Release the re-pin so a fresh attempt can pick a real victim.
                retired = repin.active
                retire(repin)
                third = track(pool.fetch(2))
                result = {"repin_value": repin_value,
                          "repin_was_active": retired,
                          "evictor_outcome": joined_evictor,
                          "during_repin_frames": snapshot,
                          "victim_not_repurposed":
                          bool(page1_frames) and not page2_present,
                          "page1_disk": disk_byte(path, 1),
                          "page2_fetch": pool.read(third).hex(),
                          "page1_still_resident_after_release":
                          any(f["page_id"] == 1 for f in pool.inspect()["frames"]),
                          "events": [e for e in pool.inspect()["events"]
                                     if e["event"] in ("evict_abandoned",
                                                       "evicted", "fetch_busy")]}

            elif name == "all-pinned":
                a = track(pool.fetch(0))
                b = track(pool.fetch(1))
                busy, bo = worker("fetch-with-all-pinned",
                                  lambda: pool.fetch(2))
                threads.append(busy)
                join(busy)
                result = {"all_pinned_error": bo.get("error"),
                          "frames": [f["page_id"] for f in
                                     pool.inspect()["frames"]],
                          "no_read_for_page_2":
                          not any(e["event"] == "begin"
                                  and e.get("page_id") == 2
                                  for e in io.inspect())}

            elif name == "close-refused":
                a = track(pool.fetch(0))
                pool.write(a, 0, b"X")
                try:
                    pool.close()
                    refused = None
                except RuntimeError as exc:
                    refused = str(exc)
                disk_before_release = disk_byte(path, 0)
                retire(a)
                pool.close()                         # now drains and closes
                result = {"close_refused": refused,
                          "disk_before_release": disk_before_release,
                          "disk_after_close": disk_byte(path, 0),
                          "io_closed": io.closed,
                          "other_pages_intact": unchanged_rest(path)}

            elif name == "close-drains-writeback":
                a = track(pool.fetch(0))
                pool.write(a, 0, b"X")
                retire(a)
                gate = io.pause_next("write", 0)
                gates.append(gate)
                flusher, fo = worker("in-flight-flush", lambda: pool.flush(0))
                threads.append(flusher)
                wait(gate.entered, "in-flight write gate")
                closer, co = worker("close-waits", pool.close)
                threads.append(closer)
                closer.join(.2)
                close_waited = closer.is_alive()
                gate.release()
                join(flusher)
                join(closer)
                result = {"close_waited_for_inflight": close_waited,
                          "flush": fo,
                          "close_error": co.get("error"),
                          "disk_after_close": disk_byte(path, 0),
                          "io_closed": io.closed}

            elif name == "close-write-failure":
                a = track(pool.fetch(0))
                pool.write(a, 0, b"X")
                retire(a)
                io.fail_next("write", 0)
                try:
                    pool.close()
                    close_error = None
                except RuntimeError as exc:
                    close_error = str(exc)
                state_after = pool.inspect()["frames"]
                disk_after_failure = disk_byte(path, 0)
                fd_still_open = not io.closed
                retried = pool.flush(0)
                pool.close()
                result = {"close_error": close_error,
                          "frames_after_failure": state_after,
                          "disk_after_failure": disk_after_failure,
                          "fd_still_open_after_failure": fd_still_open,
                          "retry_flush": retried,
                          "disk_after_retry_close": disk_byte(path, 0),
                          "io_closed_after_retry": io.closed}

            elif name == "capacity-reservation":
                # Capacity 2 counts LOADING reservations.  Two cold loads are
                # blocked in real IO (two slots reserved); a third miss must
                # report busy rather than over-allocate a third slot.
                gate5 = io.pause_next("read", 5)
                gate6 = io.pause_next("read", 6)
                gates.extend([gate5, gate6])
                loader5, lo5 = worker("page5-load_blocked", lambda: pool.fetch(5))
                loader6, lo6 = worker("page6-load_blocked", lambda: pool.fetch(6))
                threads.extend([loader5, loader6])
                wait(gate5.entered, "page-5 read gate")
                wait(gate6.entered, "page-6 read gate")
                third, so = worker("page7-miss", lambda: pool.fetch(7))
                threads.append(third)
                join(third)
                frames_during = pool.inspect()["frames"]
                created_during = pool.inspect()["created_frames"]
                reads_page7 = [e for e in io.inspect() if e.get("page_id") == 7
                               and e["event"] == "begin"]
                gate5.release()
                gate6.release()
                join(loader5)
                join(loader6)
                for outcome in (lo5, lo6):
                    if "value" in outcome:
                        track(outcome["value"])
                result = {"page7_error_while_both_loads_reserved": so.get("error"),
                          "frames_during": frames_during,
                          "created_frames_during": created_during,
                          "no_page7_read": len(reads_page7) == 0,
                          "page5_value": None if "value" not in lo5
                          else pool.read(lo5["value"]).hex(),
                          "page6_value": None if "value" not in lo6
                          else pool.read(lo6["value"]).hex()}

            else:
                raise ValueError("unknown scenario %r" % name)

            result["pool"] = pool.inspect()
            result["io"] = io.inspect()
            return result
        finally:
            for gate in gates:
                gate.release()
            for thread in threads:
                thread.join(3)
            for handle in handles:
                try:
                    if handle.active:
                        pool.release(handle)
                except Exception:
                    pass
            try:
                if not io.closed:
                    pool.close()
            except Exception:
                # Scenario already observed the failure; never mask it.
                pass


if __name__ == "__main__":
    names = (SCENARIOS if len(sys.argv) == 1 or sys.argv[1] == "all"
             else [sys.argv[1]])
    print(json.dumps({name: scenario(name) for name in names},
                     ensure_ascii=False))
