"""Correlated compound multikey index generator.

Key design points
-----------------
* Each indexed slot is stored as a **typed** ``(type_tag, value)`` pair, so
  missing, explicit null, booleans, integers and text can never collapse into
  one untyped string.  Tag ordering also gives every type a total order.
* Root scalar fields contribute one slot value each.
* All paths under one array (the only supported array is ``items``) are read
  from the **same element**, never recombined across elements.  A document
  with ``[A/1, B/5]`` produces keys ``(A,1)`` and ``(B,5)`` only - the
  nonexistent ``(A,5)`` / ``(B,1)`` combinations are never generated.
* Empty arrays and missing arrays get explicit, distinct postings
  (``E`` / ``M`` tags) so a reader can tell why a bound cannot provide
  complete coverage.
* Repeated identical keys inside one document collapse to one posting.

A compound index spanning two different array prefixes is rejected up front;
that shape is out of scope.
"""

# Type tags, ordered deliberately low -> high.  Two-character names stay
# readable inside an SQLite table.
TAG_MISSING = 'M'   # field absent (or array itself missing, on an items slot)
TAG_NULL = 'N'      # explicit JSON null
TAG_BOOL = 'B'      # JSON true / false
TAG_INT = 'I'       # JSON integer
TAG_TEXT = 'T'      # JSON text
TAG_EMPTY = 'E'     # the items array exists but has zero elements
TAG_ORDER = (TAG_MISSING, TAG_EMPTY, TAG_NULL, TAG_BOOL, TAG_INT, TAG_TEXT)


def _scalar_tag_value(value):
    if value is None:
        return TAG_NULL, None
    if isinstance(value, bool):
        return TAG_BOOL, int(value)
    if isinstance(value, int):
        return TAG_INT, value
    if isinstance(value, str):
        return TAG_TEXT, value
    raise ValueError('indexed scalar values must be null/bool/int/text, got %r' % (value,))


def split_path(path):
    pieces = path.split('.')
    if len(pieces) == 1:
        return pieces[0], None
    if len(pieces) == 2:
        return pieces[0], pieces[1]
    raise ValueError('unsupported index path: %r' % path)


class IndexSpec:
    """Validated declaration of one compound index (1..4 ordered paths)."""

    def __init__(self, paths):
        if not isinstance(paths, (list, tuple)) or not (1 <= len(paths) <= 4):
            raise ValueError('an index declares 1..4 ordered paths')
        self.paths = list(paths)
        if len(set(self.paths)) != len(self.paths):
            raise ValueError('duplicate path in index: %r' % (self.paths,))
        self.slot_paths = [split_path(path) for path in self.paths]

        array_prefixes = {array for array, sub in self.slot_paths if sub is not None}
        if len(array_prefixes) > 1:
            raise ValueError(
                'compound index spans independent array prefixes %r; '
                'cross-array multikey indexes are not supported' % sorted(array_prefixes)
            )
        self.array_name = next(iter(array_prefixes)) if array_prefixes else None
        # True for every slot that reads from the correlated array.
        self.is_array_slot = [sub is not None for _, sub in self.slot_paths]

        # Slots are visited left to right; all array slots must share the same
        # element while root slots are document-level scalars.
        self.root_slots = [
            position for position, is_array in enumerate(self.is_array_slot) if not is_array
        ]
        self.array_slots = [
            position for position, is_array in enumerate(self.is_array_slot) if is_array
        ]

    def __repr__(self):
        return 'IndexSpec(%r)' % self.paths


def validate_document(document):
    if not isinstance(document, dict):
        raise ValueError('document must be an object')
    if not isinstance(document.get('_id'), str) or not document['_id']:
        raise ValueError('document _id must be a non-empty text')
    for name, value in document.items():
        if name == 'items':
            if not isinstance(value, list):
                raise ValueError('items must be an array')
            for element in value:
                if not isinstance(element, dict):
                    raise ValueError('items elements must be objects')
                for sub_name, sub_value in element.items():
                    if isinstance(sub_value, list) or isinstance(sub_value, dict):
                        raise ValueError('nested arrays/objects are not supported (items.%s)' % sub_name)
        elif isinstance(value, (list, dict)):
            raise ValueError('root field %r must be a scalar' % name)


def _root_slot_value(document, field):
    if field not in document:
        return TAG_MISSING, None
    return _scalar_tag_value(document[field])


def _element_slot_values(element, sub):
    if sub not in element:
        return TAG_MISSING, None
    return _scalar_tag_value(element[sub])


def extract_keys(document, spec):
    """Return the de-duplicated typed index keys for one document.

    Each key is a tuple of ``(tag, value)`` pairs, one per declared slot.
    Array slots inside a key always come from the same array element.
    """
    if not isinstance(spec, IndexSpec):
        spec = IndexSpec(spec)

    root_values = {
        slot: _root_slot_value(document, spec.slot_paths[slot][0])
        for slot in spec.root_slots
    }

    keys = set()

    if not spec.array_slots:
        # Pure scalar index: exactly one key per document.
        keys.add(tuple(root_values[slot] for slot in range(len(spec.paths))))
        return sorted(keys, key=_sort_key)

    array_name = spec.array_name
    array_missing = array_name not in document

    if array_missing:
        tag = TAG_MISSING
    elif not isinstance(document[array_name], list):
        raise ValueError('%r must be an array' % array_name)
    elif len(document[array_name]) == 0:
        tag = TAG_EMPTY
    else:
        tag = None
        array = document[array_name]

    if tag is not None:
        # Whole-array placeholder: every array slot carries the same explicit
        # tag, distinct from per-element missing and from each other.
        key = []
        for slot in range(len(spec.paths)):
            key.append((tag, None) if spec.is_array_slot[slot] else root_values[slot])
        keys.add(tuple(key))
        return sorted(keys, key=_sort_key)

    # One key per element; all array slots read that single element together.
    for element in array:
        if not isinstance(element, dict):
            raise ValueError('%s elements must be objects' % array_name)
        key = []
        for slot in range(len(spec.paths)):
            if spec.is_array_slot[slot]:
                _, sub = spec.slot_paths[slot]
                key.append(_element_slot_values(element, sub))
            else:
                key.append(root_values[slot])
        keys.add(tuple(key))

    return sorted(keys, key=_sort_key)


def _sort_key(typed_key):
    # Deterministic total order over typed keys (used by tests/dumps).
    return tuple(
        (TAG_ORDER.index(tag), '' if value is None else value, isinstance(value, bool))
        for tag, value in typed_key
    )
