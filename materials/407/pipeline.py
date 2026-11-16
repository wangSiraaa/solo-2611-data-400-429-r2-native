"""Pipeline with group-local result attribution and explicit Sync planning.

The backend protocol has no request identifier.  The only safe way to attribute
a response to an application command is therefore the *order of commands inside
one Sync group* plus the response grammar for each command::

    P -> ParseComplete
    B -> BindComplete
    E -> (RowDescription DataRow*)? (CommandComplete | Error)

A Sync ends the error-skip phase and produces ReadyForQuery.  ParseComplete and
BindComplete are acknowledgements only; CommandComplete is the success terminal
and Error is the failure terminal.  Nothing is carried across a Sync boundary.

Default plan: one Sync per input cycle (the material's grouping).  An explicit
per-command plan is available through ``sync_mode='per-command'`` or
``main.py --sync per-command``.  The computed plan is returned in ``sync_plan``
and is exposed to ``plan_hook`` before any byte is sent.
"""

import json
import socket
from pathlib import Path

from backend import start_backend
from decoder import read_cycle
from wire import frame

READ_TIMEOUT = 5.0
_CLOSE_TIMEOUT = 1.0

_BATCH_NAMES = {"cycle", "batch", "per-cycle", "batch-sync"}
_COMMAND_NAMES = {"command", "per-command", "each-command", "single", "per-command-sync"}


def normalize_sync_mode(mode):
    if mode is None:
        return "cycle"
    name = str(mode).strip().lower().replace("_", "-")
    if name in _BATCH_NAMES:
        return "cycle"
    if name in _COMMAND_NAMES:
        return "per-command"
    raise ValueError("unknown sync mode: %r" % (mode,))


def load_schedule(schedule):
    """Load a schedule from a path, a JSON string, or an already-parsed dict."""

    if isinstance(schedule, dict):
        data = schedule
    elif isinstance(schedule, Path):
        data = json.loads(schedule.read_text())
    elif isinstance(schedule, str):
        stripped = schedule.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            data = json.loads(schedule)
        else:
            data = json.loads(Path(schedule).read_text())
    else:
        raise TypeError("schedule must be a path, JSON string, or dict")

    cycles = data.get("cycles")
    if not isinstance(cycles, list):
        raise ValueError("schedule must contain a 'cycles' list")
    seen = {}
    for cycle_index, cycle in enumerate(cycles):
        if not isinstance(cycle, list):
            raise ValueError("cycle %d is not a list" % cycle_index)
        for command in cycle:
            if not isinstance(command, dict):
                raise ValueError("cycle %d contains a non-object command" % cycle_index)
            if "id" not in command or "sql" not in command:
                raise ValueError("each command needs 'id' and 'sql'")
            if not isinstance(command["id"], str) or not isinstance(command["sql"], str):
                raise ValueError("'id' and 'sql' must be strings")
            if command["id"] in seen:
                raise ValueError("duplicate command id %r (cycle %d and cycle %d)"
                                 % (command["id"], seen[command["id"]], cycle_index))
            seen[command["id"]] = cycle_index
    return data


def _cycle_groups(cycle, sync_mode):
    if sync_mode == "cycle":
        # Preserve the input grouping exactly, including an empty cycle's Sync.
        return [list(cycle)]
    return [[command] for command in cycle]


def build_sync_plan(cycles, sync_mode):
    """Return the final grouping without touching a socket."""

    plan_cycles = []
    total = 0
    for cycle_index, cycle in enumerate(cycles):
        groups = _cycle_groups(cycle, sync_mode)
        ids = [[command["id"] for command in group] for group in groups]
        plan_cycles.append({
            "cycle": cycle_index,
            "commands": [command["id"] for command in cycle],
            "groups": ids,
            "sync_count": len(groups),
        })
        total += len(groups)
    return {
        "sync_mode": sync_mode,
        "cycles": plan_cycles,
        "total_syncs": total,
    }


def plan_sync(schedule, sync_mode=None):
    data = load_schedule(schedule)
    if sync_mode is None:
        sync_mode = data.get("sync_mode", "cycle")
    mode = normalize_sync_mode(sync_mode)
    return build_sync_plan(data["cycles"], mode)


def _success(command_id, columns, rows, tag):
    return {
        "id": command_id,
        "status": "success",
        "columns": list(columns),
        "rows": list(rows),
        "tag": tag,
    }


def _failure(command_id, message):
    return {
        "id": command_id,
        "status": "error",
        "message": message,
    }


def _not_executed(command_id, reason, blocked_by=None):
    result = {
        "id": command_id,
        "status": "not_executed",
        "reason": reason,
    }
    if blocked_by is not None:
        result["blocked_by"] = blocked_by
    return result


def _incomplete(command_id, reason, termination=None, ready=None, sent=True):
    return {
        "id": command_id,
        "status": "incomplete",
        "reason": reason,
        "termination": termination,
        "ready": ready,
        "sent": sent,
    }


_SKIP_REASON = (
    "server discarded this command's P/B/E after an earlier error in the "
    "same Sync group"
)


def attribute_group(commands, decoded):
    """Attribute one Sync group's decoded events to its commands, in order.

    Only the current group is considered.  A command is successful only when
    its own CommandComplete is reached after its ParseComplete and
    BindComplete; only an Error is a failure.  If an Error occurs, every later
    command in the same group is not_executed (the server ignores P/B/E until
    the next Sync).  A broken or early-ending stream leaves commands with no
    terminal evidence as ``incomplete``; it never borrows a later success.
    """

    events = decoded["events"]
    termination = decoded["termination"]
    ready = decoded["ready"]
    results = [None] * len(commands)
    error_id = None
    stop_reason = None
    position = 0

    for index, command in enumerate(commands):
        command_id = command["id"]

        if error_id is not None:
            results[index] = _not_executed(
                command_id, _SKIP_REASON, blocked_by=error_id)
            continue
        if stop_reason is not None:
            results[index] = _incomplete(
                command_id, stop_reason, termination=termination, ready=ready)
            continue

        if position >= len(events):
            stop_reason = "stream ended before this command's completion evidence"
            results[index] = _incomplete(
                command_id, stop_reason, termination=termination, ready=ready)
            continue

        event = events[position]
        if event["kind"] == "ReadyForQuery":
            stop_reason = "ReadyForQuery arrived before this command's completion evidence"
            results[index] = _incomplete(
                command_id, stop_reason, termination=termination, ready=ready)
            continue
        if event["kind"] == "Error":
            # Parse/Bind/Execute error while handling this command.  The
            # server's skip phase covers all later P/B/E in this group.
            results[index] = _failure(command_id, event["message"])
            error_id = command_id
            position += 1
            continue
        if event["kind"] != "ParseComplete":
            stop_reason = (
                "unexpected %s where this command's ParseComplete was expected"
                % event["kind"]
            )
            results[index] = _incomplete(
                command_id, stop_reason, termination=termination, ready=ready)
            continue

        position += 1
        if position >= len(events):
            stop_reason = "stream ended after ParseComplete before BindComplete"
            results[index] = _incomplete(
                command_id, stop_reason, termination=termination, ready=ready)
            continue

        event = events[position]
        if event["kind"] == "Error":
            results[index] = _failure(command_id, event["message"])
            error_id = command_id
            position += 1
            continue
        if event["kind"] != "BindComplete":
            stop_reason = (
                "unexpected %s where this command's BindComplete was expected"
                % event["kind"]
            )
            results[index] = _incomplete(
                command_id, stop_reason, termination=termination, ready=ready)
            continue

        position += 1
        columns = []
        rows = []
        terminal = None
        while position < len(events):
            event = events[position]
            if event["kind"] == "RowDescription":
                columns = list(event.get("columns", []))
                position += 1
            elif event["kind"] == "DataRow":
                rows.append(event["row"])
                position += 1
            elif event["kind"] in ("CommandComplete", "Error"):
                terminal = event
                position += 1
                break
            else:
                break

        if terminal is None:
            if position < len(events):
                stop_reason = (
                    "%s arrived before this command's CommandComplete/Error"
                    % events[position]["kind"]
                )
            else:
                stop_reason = "stream ended before this command's CommandComplete/Error"
            results[index] = _incomplete(
                command_id, stop_reason, termination=termination, ready=ready)
            continue

        if terminal["kind"] == "Error":
            results[index] = _failure(command_id, terminal["message"])
            error_id = command_id
        else:
            results[index] = _success(
                command_id, columns, rows, terminal["tag"])

    for index, result in enumerate(results):
        if result is None:
            results[index] = _incomplete(
                commands[index]["id"], "no attribution produced",
                termination=termination, ready=ready)
    return results


def _frontend_messages(command):
    name = command["id"]
    return [
        ("P", (name + "\0" + command["sql"]).encode()),
        ("B", ("portal-" + name + "\0" + name).encode()),
        ("E", ("portal-" + name).encode()),
    ]


def _send_entry(kind, command_id=None):
    entry = {"kind": kind}
    if command_id is not None:
        entry["id"] = command_id
    return entry


def _close_backend(sock, thread):
    """Best-effort X/close, then join the backend thread with a bound."""

    try:
        sock.settimeout(_CLOSE_TIMEOUT)
    except OSError:
        pass
    try:
        sock.sendall(frame("X"))
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
    try:
        thread.join(READ_TIMEOUT)
    except RuntimeError:
        pass
    return not thread.is_alive()


def run(schedule, sync_mode=None, plan_hook=None):
    data = load_schedule(schedule)
    if sync_mode is None:
        sync_mode = data.get("sync_mode", "cycle")
    mode = normalize_sync_mode(sync_mode)
    cycles = data["cycles"]
    sync_plan = build_sync_plan(cycles, mode)

    # Publish the final grouping before the backend is started or any byte is
    # sent.  The same plan is returned in the result.
    if plan_hook is not None:
        plan_hook(sync_plan)

    sock, thread, audit = start_backend()
    try:
        sock.settimeout(READ_TIMEOUT)
    except OSError:
        pass

    records = {}
    send_log = []
    receive_log = []
    cycle_reports = []
    aborted = False
    abort_reason = None
    sync_index = 0

    def remember(command, result):
        # Command ids are specified to be unique; setdefault also keeps a
        # later "not sent" sweep from overwriting real packet evidence.
        records.setdefault(command["id"], result)

    def note_send(cycle_report, group_report, cycle_number, kind, command_id=None):
        entry = _send_entry(kind, command_id)
        send_log.append({
            "seq": len(send_log) + 1,
            "cycle": cycle_number,
            "sync": sync_index,
            **entry,
        })
        cycle_report["send_order"].append(dict(entry))
        group_report["send_order"].append(dict(entry))

    try:
        for cycle_number, cycle in enumerate(cycles):
            cycle_report = {
                "cycle": cycle_number,
                "commands": [command["id"] for command in cycle],
                "send_order": [],
                "receive_order": [],
                "sync_groups": [],
                "ready_states": [],
                "transaction": None,
                "termination": "ready",
                "error": None,
            }

            if aborted:
                for command in cycle:
                    remember(command, _not_executed(
                        command["id"],
                        "not sent: pipeline stopped after an earlier "
                        "stream failure (%s)" % abort_reason,
                    ))
                cycle_report["termination"] = "not_sent"
                cycle_report["error"] = abort_reason
                cycle_reports.append(cycle_report)
                continue

            groups = _cycle_groups(cycle, mode)
            for group_number, group in enumerate(groups):
                if aborted:
                    for command in group:
                        remember(command, _not_executed(
                            command["id"],
                            "not sent: pipeline stopped after an earlier "
                            "stream failure (%s)" % abort_reason,
                        ))
                    continue

                sync_index += 1
                group_report = {
                    "sync_index": sync_index,
                    "commands": [command["id"] for command in group],
                    "send_order": [],
                    "receive_order": [],
                    "ready": None,
                    "termination": None,
                    "error": None,
                    "events": [],
                }

                try:
                    for command in group:
                        for kind, payload in _frontend_messages(command):
                            sock.sendall(frame(kind, payload))
                            note_send(cycle_report, group_report, cycle_number,
                                      kind, command["id"])
                    sock.sendall(frame("S"))
                    note_send(cycle_report, group_report, cycle_number, "S")
                except OSError as error:
                    aborted = True
                    abort_reason = "send error: %s" % error
                    cycle_report["termination"] = "io_error"
                    cycle_report["error"] = abort_reason
                    group_report["termination"] = "send_error"
                    group_report["error"] = abort_reason
                    sent_ids = {
                        entry["id"] for entry in group_report["send_order"]
                        if entry["kind"] == "P"
                    }
                    for command in group:
                        if command["id"] in sent_ids:
                            remember(command, _incomplete(
                                command["id"],
                                "command was sent but no Sync/response "
                                "completed before a send error; execution is "
                                "unknown",
                                termination="io_error",
                                sent=True,
                            ))
                        else:
                            remember(command, _not_executed(
                                command["id"],
                                "not sent: a send error occurred before this "
                                "command's P/B/E and the group never reached Sync",
                            ))
                    for later_group in groups[group_number + 1:]:
                        for command in later_group:
                            remember(command, _not_executed(
                                command["id"],
                                "not sent: pipeline stopped after a send error",
                            ))
                    cycle_report["sync_groups"].append(group_report)
                    break

                decoded = read_cycle(sock)
                group_report["ready"] = decoded["ready"]
                group_report["termination"] = decoded["termination"]
                group_report["error"] = decoded["error"]
                group_report["events"] = decoded["events"]
                group_report["receive_order"] = [
                    event["wire_type"] for event in decoded["events"]
                ]
                for event in decoded["events"]:
                    entry = {
                        "seq": len(receive_log) + 1,
                        "cycle": cycle_number,
                        "sync": sync_index,
                        "kind": event["wire_type"],
                        "event": event["kind"],
                    }
                    if event["kind"] == "ReadyForQuery":
                        entry["transaction"] = event["transaction"]
                    receive_log.append(entry)
                    cycle_report["receive_order"].append(event["wire_type"])

                for command, result in zip(group, attribute_group(group, decoded)):
                    remember(command, result)

                cycle_report["sync_groups"].append(group_report)
                cycle_report["ready_states"].append(decoded["ready"])
                if decoded["ready"] is not None:
                    cycle_report["transaction"] = decoded["ready"]

                if decoded["termination"] != "ready":
                    aborted = True
                    abort_reason = "%s: %s" % (
                        decoded["termination"], decoded["error"])
                    cycle_report["termination"] = decoded["termination"]
                    cycle_report["error"] = decoded["error"]
                    for later_group in groups[group_number + 1:]:
                        for command in later_group:
                            remember(command, _not_executed(
                                command["id"],
                                "not sent: pipeline stopped after an earlier "
                                "stream failure (%s)" % abort_reason,
                            ))
                    break

            cycle_reports.append(cycle_report)
    finally:
        backend_stopped = _close_backend(sock, thread)

    ordered_results = []
    for cycle in cycles:
        for command in cycle:
            result = records.get(command["id"])
            if result is None:
                result = _incomplete(
                    command["id"], "no record produced by the pipeline")
            ordered_results.append(result)

    unanswered = [
        result["id"] for result in ordered_results
        if result["status"] == "incomplete"
    ]

    return {
        "sync_mode": mode,
        "sync_plan": sync_plan,
        "cycles": cycle_reports,
        "results": ordered_results,
        "unanswered": unanswered,
        "send_log": send_log,
        "receive_log": receive_log,
        "backend_audit": list(audit),
        "backend_thread_stopped": backend_stopped,
        "read_timeout_seconds": READ_TIMEOUT,
    }
