"""Event-preserving decoder for the extended-query wire subset.

This module deliberately does not decide which application command an event
belongs to.  It only turns bytes into ordered events and records how the read
ended:

* ParseComplete ('1') and BindComplete ('2') acknowledge Parse and Bind; they
  are not evidence that the SQL completed.
* CommandComplete ('C') and Error ('E') are different terminal event kinds.
* ReadyForQuery ('Z') carries the transaction state and is the only event that
  closes the skip-until-Sync phase.

A truncated frame, EOF, timeout, or protocol error is reported as such.  The
caller must not turn an unclosed stream into success or into a claim that a
command was skipped.
"""

import json

from wire import receive

_WIRE_TYPES = {
    "ParseComplete": "1",
    "BindComplete": "2",
    "RowDescription": "T",
    "DataRow": "D",
    "CommandComplete": "C",
    "Error": "E",
    "ReadyForQuery": "Z",
}


def _decode(kind, payload):
    if kind == "1":
        return {"kind": "ParseComplete"}
    if kind == "2":
        return {"kind": "BindComplete"}
    if kind == "T":
        return {"kind": "RowDescription", "columns": json.loads(payload)}
    if kind == "D":
        return {"kind": "DataRow", "row": json.loads(payload)}
    if kind == "C":
        return {"kind": "CommandComplete", "tag": payload.decode("ascii")}
    if kind == "E":
        return {"kind": "Error", "message": payload.decode("utf-8", "replace")}
    if kind == "Z":
        return {"kind": "ReadyForQuery",
                "transaction": payload.decode("ascii", "replace")}
    return {"kind": "Unknown", "wire_type": kind, "payload_hex": payload.hex()}


def _with_wire_type(event):
    event = dict(event)
    event["wire_type"] = _WIRE_TYPES.get(event["kind"], "?")
    return event


def read_cycle(sock):
    """Read one Sync group's responses through its ReadyForQuery.

    Returns::

        {
          "events": [{...ordered events...}, ...],
          "ready": "I" | "T" | "E" | None,
          "termination": "ready" | "eof" | "timeout" | "io_error"
                         | "protocol_error",
          "error": None or diagnostic string,
        }

    ``ready`` is None unless a complete ReadyForQuery was decoded.  Events seen
    before a broken stream are retained: they are genuine evidence for the
    commands that produced them, but they may not be extended across the
    missing Sync boundary.
    """

    events = []
    try:
        while True:
            kind, payload = receive(sock)
            event = _with_wire_type(_decode(kind, payload))
            events.append(event)
            if event["kind"] == "ReadyForQuery":
                return {
                    "events": events,
                    "ready": event["transaction"],
                    "termination": "ready",
                    "error": None,
                }
    except EOFError as exc:
        return {
            "events": events,
            "ready": None,
            "termination": "eof",
            "error": str(exc),
        }
    except TimeoutError as exc:
        return {
            "events": events,
            "ready": None,
            "termination": "timeout",
            "error": str(exc) or "socket read timed out",
        }
    except ValueError as exc:
        return {
            "events": events,
            "ready": None,
            "termination": "protocol_error",
            "error": str(exc),
        }
    except OSError as exc:
        return {
            "events": events,
            "ready": None,
            "termination": "io_error",
            "error": str(exc),
        }
