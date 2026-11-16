"""The small incremental-response dialect used by this exercise.

The module intentionally knows nothing about HTTP, MIME types, GraphQL
execution or the many historical incremental-delivery drafts.  It validates
the envelopes described by ``protocol.json``, applies a frame's patches to an
in-memory JSON tree, and derives order/line relations (with provenance) from
that tree.
"""

from __future__ import annotations

import copy
import json
from typing import Any

MAX_PATH_DEPTH = 5
MAX_ORDERS = 20
MAX_LINES_PER_ORDER = 50


class ProtocolError(Exception):
    """The envelope or the resulting document violates the stated contract."""


class DuplicateIdentity(ProtocolError):
    """Two non-null orders or two lines in one order share a business id."""


def _bad(message: str) -> None:
    raise ProtocolError(message)


def _is_int(value: Any) -> bool:
    # bool is an int subclass; it is not a valid JSON array index here.
    return type(value) is int


def _checked_path(path: Any) -> tuple:
    if not isinstance(path, list):
        _bad("path must be a JSON array")
    if len(path) > MAX_PATH_DEPTH:
        _bad("path depth exceeds %d" % MAX_PATH_DEPTH)
    for part in path:
        if isinstance(part, str):
            continue
        if _is_int(part) and part >= 0:
            continue
        _bad("path elements must be strings or non-negative integers")
    return tuple(path)


def _errors(value: Any) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        _bad("errors must be an array")
    return copy.deepcopy(value)


def validate_envelope(envelope: Any, first: bool) -> None:
    """Validate one already-decoded envelope.

    ``first`` selects the initial-frame shape.  The application never peeks at
    FrameSource internals to decide which frame it is reading.
    """
    if not isinstance(envelope, dict):
        _bad("an envelope must be a JSON object")
    if "hasNext" not in envelope or type(envelope.get("hasNext")) is not bool:
        _bad("hasNext must be a Boolean")
    _errors(envelope.get("errors"))

    if first:
        if "data" not in envelope:
            _bad("the first envelope must contain data")
        data = envelope.get("data")
        if data is not None and not isinstance(data, dict):
            _bad("the first data value must be an object or null")
        incremental = envelope.get("incremental")
        if incremental is not None:
            if not isinstance(incremental, list):
                _bad("incremental must be an array")
            if incremental:
                _bad("the first envelope cannot carry incremental entries")
        return

    if "data" in envelope:
        _bad("later envelopes carry patches in incremental, not data")
    incremental = envelope.get("incremental")
    if incremental is None:
        return
    if not isinstance(incremental, list):
        _bad("incremental must be an array")
    for entry in incremental:
        if not isinstance(entry, dict):
            _bad("incremental entries must be objects")
        if "path" not in entry:
            _bad("an incremental entry requires path")
        _checked_path(entry["path"])
        has_data = "data" in entry
        has_items = "items" in entry
        if has_data == has_items:
            _bad("an incremental entry requires exactly one of data or items")
        if has_data:
            data = entry["data"]
            if data is not None and not isinstance(data, dict):
                _bad("patch data must be an object or null")
        else:
            if not isinstance(entry["items"], list):
                _bad("items must be an array")
        _errors(entry.get("errors"))


def _resolve(root: Any, path: tuple) -> Any:
    current = root
    for part in path:
        if isinstance(current, dict):
            if not isinstance(part, str) or part not in current:
                _bad("path does not identify an existing container")
            current = current[part]
        elif isinstance(current, list):
            if not _is_int(part) or part < 0 or part >= len(current):
                _bad("path does not identify an existing list element")
            current = current[part]
        else:
            _bad("path traverses a scalar value")
    return current


def _parent_for_patch(root: Any, path: tuple) -> tuple:
    """Resolve a patch's parent, allowing an absent object field.

    ``items`` insertions are separate; a ``data`` patch never appends to a
    list, so an out-of-range list index stays invalid.
    """
    parent = _resolve(root, path[:-1])
    key = path[-1]
    if isinstance(parent, dict):
        if not isinstance(key, str):
            _bad("object fields are addressed by string path elements")
    elif isinstance(parent, list):
        if not _is_int(key) or key < 0 or key >= len(parent):
            _bad("path does not identify an existing list element")
    else:
        _bad("path traverses a scalar value")
    return parent, key


def apply_entry(tree: Any, entry: dict) -> Any:
    """Apply one incremental entry to ``tree`` and return the resulting tree.

    Null list elements remain list elements; patches never close an index gap.
    """
    path = tuple(entry["path"])
    if "items" in entry:
        if not path or not _is_int(path[-1]):
            _bad("items requires a list and an insertion position at path end")
        position = path[-1]
        container = _resolve(tree, path[:-1])
        if not isinstance(container, list):
            _bad("items insertion target is not a list")
        if position < 0 or position > len(container):
            _bad("items insertion position is outside the list")
        container[position:position] = copy.deepcopy(entry["items"])
        return tree

    data = entry["data"]
    if not path:
        # An empty path patches the root itself.
        if data is None:
            return None
        if not isinstance(tree, dict):
            _bad("object data cannot merge into a non-object root")
        for key, value in copy.deepcopy(data).items():
            tree[key] = value
        return tree

    parent, key = _parent_for_patch(tree, path)
    absent = isinstance(parent, dict) and key not in parent
    if data is None:
        if absent:
            _bad("path does not identify an existing object field")
        parent[key] = None
    elif absent:
        # An absent object field may be added by an object patch.
        parent[key] = copy.deepcopy(data)
    else:
        target = parent[key]
        if not isinstance(target, dict):
            _bad("object data must merge into an existing object")
        for field, value in copy.deepcopy(data).items():
            target[field] = value
    return tree


def apply_frame(tree: Any, envelope: dict) -> Any:
    """Apply every entry of a non-initial frame to a private copy of ``tree``."""
    result = copy.deepcopy(tree)
    for entry in envelope.get("incremental") or []:
        result = apply_entry(result, entry)
    return result


def frame_errors(envelope: dict) -> list:
    """GraphQL errors carried by an envelope, preserving their paths."""
    errors = _errors(envelope.get("errors"))
    for entry in envelope.get("incremental") or []:
        errors.extend(_errors(entry.get("errors")))
    return errors


def extract_relations(tree: Any, stats: dict | None = None) -> tuple[dict, dict]:
    """Derive orders and lines from the current JSON tree.

    Returns ``(orders, lines)`` where orders maps business id to
    ``{'path': tuple, 'tax': int|None}`` and lines maps
    ``(order_id, line_id)`` to ``{'path': tuple, 'qty': int|None}``.
    Duplicate business identities are protocol errors.

    ``stats['nodes']`` counts the relation nodes visited by this derivation;
    the session records it to make the cost of a full materialization visible.
    """
    if stats is not None:
        stats.setdefault('nodes', 0)
    orders: dict[str, dict] = {}
    lines: dict[tuple[str, str], dict] = {}
    if tree is None:
        return orders, lines
    if not isinstance(tree, dict):
        _bad("document root must be an object")

    order_list = tree.get("orders")
    if order_list is None:
        return orders, lines
    if not isinstance(order_list, list):
        _bad("orders must be a list or null")
    if len(order_list) > MAX_ORDERS:
        _bad("orders exceeds the bound of %d" % MAX_ORDERS)

    for order_index, order in enumerate(order_list):
        if stats is not None:
            stats['nodes'] += 1
        if order is None:
            continue
        if not isinstance(order, dict):
            _bad("a non-null order must be an object")
        order_id = order.get("id")
        if not isinstance(order_id, str) or order_id == "":
            _bad("a non-null order requires a nonempty string id")
        if order_id in orders:
            raise DuplicateIdentity("duplicate order id %r" % order_id)
        tax = order.get("tax")
        if tax is not None and not _is_int(tax):
            _bad("order tax must be an integer or null")

        order_path = ("orders", order_index)
        orders[order_id] = {"path": order_path, "tax": tax}

        line_list = order.get("lines")
        if line_list is None:
            continue
        if not isinstance(line_list, list):
            _bad("order lines must be a list or null")
        if len(line_list) > MAX_LINES_PER_ORDER:
            _bad("order lines exceeds the bound of %d" % MAX_LINES_PER_ORDER)
        seen_lines: set[str] = set()
        for line_index, line in enumerate(line_list):
            if stats is not None:
                stats['nodes'] += 1
            if line is None:
                continue
            if not isinstance(line, dict):
                _bad("a non-null line must be an object")
            line_id = line.get("id")
            if not isinstance(line_id, str) or line_id == "":
                _bad("a non-null line requires a nonempty string id")
            if line_id in seen_lines:
                raise DuplicateIdentity(
                    "duplicate line id %r in order %r" % (line_id, order_id))
            seen_lines.add(line_id)
            qty = line.get("qty")
            if qty is not None and not _is_int(qty):
                _bad("line qty must be an integer or null")
            lines[(order_id, line_id)] = {
                "path": ("orders", order_index, "lines", line_index),
                "qty": qty,
            }
    return orders, lines


def reconcile(
    old_orders: dict,
    old_lines: dict,
    tree: Any,
    frame: int,
    stats: dict | None = None,
) -> tuple[dict, dict]:
    """Materialize ``tree`` and carry provenance over from the last frame.

    A row keeps ``created_frame`` when its business identity survives.  Its
    ``last_changed_frame`` moves only when the row's value or source path
    changed; a full rebuild therefore does not touch unchanged rows.
    """
    new_orders, new_lines = extract_relations(tree, stats)

    if stats is not None:
        stats.setdefault('order_changes', {'added': 0, 'changed': 0,
                                           'removed': 0, 'unchanged': 0})
        stats.setdefault('line_changes', {'added': 0, 'changed': 0,
                                          'removed': 0, 'unchanged': 0})

    orders: dict[str, dict] = {}
    for order_id, current in new_orders.items():
        previous = old_orders.get(order_id)
        if previous is None:
            created = changed = frame
            if stats is not None:
                stats['order_changes']['added'] += 1
        else:
            created = previous["created_frame"]
            moved = (
                previous["path"] != current["path"]
                or previous["tax"] != current["tax"]
            )
            changed = frame if moved else previous["last_changed_frame"]
            if stats is not None:
                stats['order_changes']['changed' if moved else 'unchanged'] += 1
        orders[order_id] = {
            "path": current["path"],
            "tax": current["tax"],
            "created_frame": created,
            "last_changed_frame": changed,
        }
    if stats is not None:
        stats['order_changes']['removed'] = len(
            set(old_orders) - set(new_orders))

    lines: dict[tuple[str, str], dict] = {}
    for key, current in new_lines.items():
        previous = old_lines.get(key)
        if previous is None:
            created = changed = frame
            if stats is not None:
                stats['line_changes']['added'] += 1
        else:
            created = previous["created_frame"]
            moved = (
                previous["path"] != current["path"]
                or previous["qty"] != current["qty"]
            )
            changed = frame if moved else previous["last_changed_frame"]
            if stats is not None:
                stats['line_changes']['changed' if moved else 'unchanged'] += 1
        lines[key] = {
            "path": current["path"],
            "qty": current["qty"],
            "created_frame": created,
            "last_changed_frame": changed,
        }
    if stats is not None:
        stats['line_changes']['removed'] = len(
            set(old_lines) - set(new_lines))
        stats['rows_added'] = (stats['order_changes']['added']
                               + stats['line_changes']['added'])
        stats['rows_changed'] = (stats['order_changes']['changed']
                                 + stats['line_changes']['changed'])
        stats['rows_removed'] = (stats['order_changes']['removed']
                                 + stats['line_changes']['removed'])
        stats['rows_unchanged'] = (stats['order_changes']['unchanged']
                                   + stats['line_changes']['unchanged'])
        # Minimal logical writes for an incremental path-dependency index.
        stats['minimal_writes'] = (stats['rows_added'] + stats['rows_changed']
                                   + stats['rows_removed'])
    return orders, lines


def path_text(path: tuple) -> str:
    """SQLite representation of a source path: a compact JSON array string."""
    return json.dumps(list(path), separators=(",", ":"), ensure_ascii=False)
