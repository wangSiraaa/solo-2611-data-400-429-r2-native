"""Independent checks for the extended-query pipeline.

Run from this directory::

    python3 verify.py

The pipeline's production attribution uses only received wire events.  This
script cross-checks those results against the backend's independent audit and
the final SQLite read values, and exercises broken-stream handling.
"""

import json
import socket
import threading
from pathlib import Path

from decoder import read_cycle
from pipeline import plan_sync, run
from wire import frame

FIXTURES = Path(__file__).with_name("fixtures")


def _by_id(result):
    return {entry["id"]: entry for entry in result["results"]}


def _executed(audit):
    return [entry["executed_sql"] for entry in audit if "executed_sql" in entry]


def _check(condition, message):
    if not condition:
        raise AssertionError(message)


def check_normal():
    result = run(FIXTURES / "normal.json")
    records = _by_id(result)
    _check(records["q1"]["status"] == "success", "normal q1 should succeed")
    _check(records["q1"]["rows"] == [[10]], "normal q1 should read 10")
    _check(records["q1"]["columns"] == ["value"], "normal q1 column name")
    _check(records["q2"]["rows"] == [[11]], "normal q2 should read 11")
    _check(records["q2"]["tag"] == "SELECT", "normal q2 tag")
    _check(result["unanswered"] == [], "normal has no unanswered commands")
    _check(result["cycles"][0]["ready_states"] == ["I"], "normal ready I")
    _check(len(_executed(result["backend_audit"])) == 2, "normal executes 2")
    _check(result["backend_thread_stopped"], "normal backend thread stops")
    print("normal: ok")


def check_skip():
    result = run(FIXTURES / "skip.json")
    records = _by_id(result)
    _check(records["before"]["rows"] == [[10]], "skip before reads 10")
    _check(records["bad"]["status"] == "error", "skip bad errors")
    _check(records["ignored"]["status"] == "not_executed",
           "skipped UPDATE must be not_executed")
    _check(records["ignored"]["blocked_by"] == "bad",
           "skipped UPDATE names its blocker")
    _check(records["after"]["status"] == "success",
           "after belongs to cycle 1")
    _check(records["after"]["rows"] == [[10]],
           "ignored UPDATE must not have changed the value")
    _check(result["cycles"][0]["ready_states"] == ["I"], "skip cycle0 ready I")
    _check(result["cycles"][1]["ready_states"] == ["I"], "skip cycle1 ready I")
    # Independent audit: the skipped UPDATE never reached SQLite, while the
    # separate after cycle did execute.
    executed = _executed(result["backend_audit"])
    _check("UPDATE counter SET value=value+100" not in executed,
           "backend audit must not show the skipped UPDATE")
    _check(executed.count("SELECT value FROM counter") == 2,
           "backend audit shows before and after SELECT")
    _check(result["unanswered"] == [], "skip has no unanswered commands")
    _check(result["backend_thread_stopped"], "skip backend thread stops")
    print("skip: ok")


def check_aborted():
    result = run(FIXTURES / "aborted.json")
    records = _by_id(result)
    _check(records["begin"]["tag"] == "BEGIN", "aborted begin tag")
    _check(records["bad"]["status"] == "error", "aborted bad errors")
    _check(records["ignored"]["status"] == "not_executed",
           "aborted ignored not executed")
    _check(records["still-aborted"]["status"] == "error",
           "aborted transaction keeps rejecting business queries")
    _check("25P02" in records["still-aborted"]["message"],
           "aborted query reports the aborted transaction")
    _check(records["rollback"]["tag"] == "ROLLBACK", "rollback succeeds")
    _check(records["read"]["rows"] == [[10]], "final read is 10")
    _check([cycle["ready_states"] for cycle in result["cycles"]]
           == [["E"], ["E"], ["I"], ["I"]],
           "per-cycle ReadyForQuery transaction states")
    _check(result["unanswered"] == [], "aborted has no unanswered commands")
    _check(result["backend_thread_stopped"], "aborted backend thread stops")
    print("aborted: ok")


def check_per_command_skip():
    result = run(FIXTURES / "skip.json", "per-command")
    records = _by_id(result)
    _check(result["sync_plan"]["total_syncs"] == 4,
           "per-command plan issues one Sync per command")
    _check(records["bad"]["status"] == "error", "per-command bad errors")
    _check(records["ignored"]["status"] == "success",
           "per-command mode runs the UPDATE that batch mode skips")
    _check(records["ignored"]["tag"] == "UPDATE", "per-command UPDATE tag")
    _check(records["after"]["rows"] == [[110]],
           "per-command mode observes the UPDATE")
    print("per-command skip: ok")


def check_plan_published_before_send():
    seen = {}

    def hook(plan):
        seen["plan"] = plan
        seen["finite_wait"] = True

    result = run(FIXTURES / "skip.json", "cycle", plan_hook=hook)
    _check(seen["plan"] == result["sync_plan"],
           "plan hook saw the final grouping")
    _check(seen["plan"]["cycles"][0]["groups"] == [["before", "bad", "ignored"]],
           "default grouping preserves the input cycle")
    _check(plan_sync(FIXTURES / "skip.json", "cycle") == result["sync_plan"],
           "plan_sync agrees with the run")
    print("plan: ok")


def check_stress():
    cycles = []
    expected = []
    for cycle in range(4):
        commands = []
        for command in range(8):
            value = cycle * 8 + command + 1
            commands.append({"id": "c%dq%d" % (cycle, command),
                             "sql": "SELECT %d" % value})
            expected.append(value)
        cycles.append(commands)
    schedule = {"cycles": cycles}
    result = run(schedule)
    _check(len(result["results"]) == 32, "stress has 32 results")
    _check(all(entry["status"] == "success" for entry in result["results"]),
           "stress commands all succeed")
    _check([entry["rows"][0][0] for entry in result["results"]] == expected,
           "stress results stay in send order")
    _check(len(result["send_log"]) == 4 * (8 * 3 + 1),
           "stress sent P/B/E for every command plus one Sync")
    _check(result["sync_plan"]["total_syncs"] == 4,
           "stress default plan is one Sync per cycle")
    print("stress (4 cycles x 8 commands): ok")


def _decode_against(feed):
    client, server = socket.socketpair()
    feeder = threading.Thread(target=feed, args=(server,))
    feeder.start()
    try:
        return read_cycle(client)
    finally:
        client.close()
        feeder.join()
        server.close()


def check_broken_stream():
    def truncated(server):
        server.sendall(frame("1") + frame("2") + frame("C", b"SELECT"))
        server.sendall(frame("1")[:3])
        server.close()

    decoded = _decode_against(truncated)
    _check(decoded["termination"] == "eof", "half frame ends as eof")
    _check(decoded["ready"] is None, "half frame has no ReadyForQuery")
    _check([event["kind"] for event in decoded["events"]]
           == ["ParseComplete", "BindComplete", "CommandComplete"],
           "a partial frame yields no event")

    def silent(server):
        import time
        time.sleep(0.4)
        server.close()

    client, server = socket.socketpair()
    client.settimeout(0.15)
    feeder = threading.Thread(target=silent, args=(server,))
    feeder.start()
    try:
        decoded = read_cycle(client)
    finally:
        client.close()
        feeder.join()
        server.close()
    _check(decoded["termination"] == "timeout", "silence ends as timeout")
    _check(decoded["ready"] is None, "timeout has no ReadyForQuery")
    _check(decoded["events"] == [], "timeout proves no execution")
    print("broken stream / timeout: ok")


def main():
    check_normal()
    check_skip()
    check_aborted()
    check_per_command_skip()
    check_plan_published_before_send()
    check_stress()
    check_broken_stream()
    print("all checks passed")


if __name__ == "__main__":
    main()
