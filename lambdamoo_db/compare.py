"""
Database comparison module for lambdamoo-db-py.

Provides structured diffs between two MooDatabase instances.
"""

from __future__ import annotations
import enum
import math
import struct
from functools import cmp_to_key
from typing import Any, Iterator
import attrs

from .database import Activation, Clear, InterruptedTask, MooDatabase, MooObject, Property, QueuedTask, SuspendedTask, VM, Verb, Waif, WaifReference


_SAVED_STATE_TYPES = (Activation, VM, QueuedTask, SuspendedTask, InterruptedTask)
_UNSET = object()


class DiffKind(enum.Enum):
    """Classification of difference types."""

    VALUE_CHANGED = "changed"
    TYPE_MISMATCH = "type_mismatch"
    MISSING = "missing"
    EXTRA = "extra"
    LENGTH_MISMATCH = "length_mismatch"


@attrs.define(frozen=True)
class DiffPath:
    """Immutable path to a location within a MOO database structure."""

    segments: tuple[str | int, ...] = attrs.field(factory=tuple)

    def __str__(self) -> str:
        """Render as human-readable path like '#123.properties[0].value'."""
        if not self.segments:
            return "<root>"

        result = []
        for seg in self.segments:
            if isinstance(seg, int):
                result.append(f"[{seg}]")
            elif result:  # Not first segment, add dot separator
                result.append(f".{seg}")
            else:
                result.append(str(seg))
        return "".join(result)

    def child(self, segment: str | int) -> DiffPath:
        """Create child path by appending segment."""
        return DiffPath(self.segments + (segment,))

    @classmethod
    def root(cls) -> DiffPath:
        """Create root path."""
        return cls(())

    @classmethod
    def object(cls, obj_id: int) -> DiffPath:
        """Create path starting at an object."""
        return cls((f"#{obj_id}",))


@attrs.define(frozen=True)
class Diff:
    """A single difference between two database structures."""

    path: DiffPath
    kind: DiffKind
    expected: Any = None
    actual: Any = None

    def __str__(self) -> str:
        match self.kind:
            case DiffKind.VALUE_CHANGED:
                return f"{self.path}: {self.expected!r} -> {self.actual!r}"
            case DiffKind.TYPE_MISMATCH:
                exp_name = self.expected.__name__ if hasattr(self.expected, '__name__') else str(self.expected)
                act_name = self.actual.__name__ if hasattr(self.actual, '__name__') else str(self.actual)
                return f"{self.path}: type {exp_name} -> {act_name}"
            case DiffKind.MISSING:
                return f"{self.path}: MISSING (expected {self.expected!r})"
            case DiffKind.EXTRA:
                return f"{self.path}: EXTRA (got {self.actual!r})"
            case DiffKind.LENGTH_MISMATCH:
                return f"{self.path}: length {self.expected} -> {self.actual}"
            case _:
                return f"{self.path}: {self.kind.value}"


@attrs.define
class CompareResult:
    """Result of comparing two databases."""

    diffs: list[Diff] = attrs.field(factory=list)

    @property
    def identical(self) -> bool:
        """True if no differences were found."""
        return len(self.diffs) == 0

    def __bool__(self) -> bool:
        """Truthy if there are differences."""
        return not self.identical

    def __len__(self) -> int:
        """Number of differences."""
        return len(self.diffs)

    def __iter__(self) -> Iterator[Diff]:
        """Iterate over differences."""
        return iter(self.diffs)

    def filter_by_kind(self, kind: DiffKind) -> list[Diff]:
        """Return only diffs of the specified kind."""
        return [d for d in self.diffs if d.kind == kind]

    def filter_by_path_prefix(self, prefix: str) -> list[Diff]:
        """Return only diffs whose path starts with the given prefix."""
        return [d for d in self.diffs if str(d.path).startswith(prefix)]

    def summary(self) -> str:
        """Generate human-readable summary."""
        if self.identical:
            return "Databases are identical"

        by_kind: dict[DiffKind, list[Diff]] = {}
        for d in self.diffs:
            by_kind.setdefault(d.kind, []).append(d)

        lines = [f"Found {len(self.diffs)} difference(s):"]
        for kind, diffs in by_kind.items():
            lines.append(f"  {kind.value}: {len(diffs)}")
        return "\n".join(lines)

    def report(self, max_diffs: int = 50) -> str:
        """Generate detailed human-readable report."""
        if self.identical:
            return "Databases are identical"

        lines = [self.summary(), ""]
        for i, diff in enumerate(self.diffs[:max_diffs]):
            lines.append(f"  {i + 1}. {diff}")

        if len(self.diffs) > max_diffs:
            lines.append(f"  ... and {len(self.diffs) - max_diffs} more")

        return "\n".join(lines)


def compare_values(path: DiffPath, expected: Any, actual: Any) -> list[Diff]:
    """
    Compare two MOO values, returning all differences.

    Handles primitives, wrapper types (ObjNum, Anon, etc.), and collections (list, dict).
    """
    # Handle None specially
    if expected is None and actual is None:
        return []
    if expected is None or actual is None:
        return [Diff(path, DiffKind.VALUE_CHANGED, expected, actual)]

    # Type mismatch check - distinguish int from ObjNum, etc.
    if type(expected) is not type(actual):
        return [Diff(path, DiffKind.TYPE_MISMATCH, type(expected), type(actual))]

    # Handle Clear singleton
    if isinstance(expected, Clear):
        return []  # Both are Clear singleton

    # Handle WaifReference
    if isinstance(expected, WaifReference):
        if expected.index != actual.index:
            return [Diff(path, DiffKind.VALUE_CHANGED, expected, actual)]
        return []

    # Handle lists
    if isinstance(expected, (list, tuple)):
        return _compare_lists(path, expected, actual)

    # Handle dicts/maps
    if isinstance(expected, dict):
        return _compare_dicts(path, expected, actual)

    if isinstance(expected, _SAVED_STATE_TYPES):
        return _compare_attrs(path, expected, actual)

    # Handle floats with tolerance
    if isinstance(expected, float):
        if math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-12):
            return []
        return [Diff(path, DiffKind.VALUE_CHANGED, expected, actual)]

    # Primitives and wrapper types (int, str, bool, ObjNum, Anon, MooError, etc.)
    if expected != actual:
        return [Diff(path, DiffKind.VALUE_CHANGED, expected, actual)]

    return []


def _compare_lists(path: DiffPath, expected: list | tuple, actual: list | tuple) -> list[Diff]:
    """Compare two lists element by element."""
    diffs: list[Diff] = []

    # Length difference
    if len(expected) != len(actual):
        diffs.append(Diff(path, DiffKind.LENGTH_MISMATCH, len(expected), len(actual)))

    # Compare elements up to the shorter length
    for i in range(min(len(expected), len(actual))):
        diffs.extend(compare_values(path.child(i), expected[i], actual[i]))

    # Missing elements (in expected but not actual)
    for i in range(len(actual), len(expected)):
        diffs.append(Diff(path.child(i), DiffKind.MISSING, expected[i], None))

    # Extra elements (in actual but not expected)
    for i in range(len(expected), len(actual)):
        diffs.append(Diff(path.child(i), DiffKind.EXTRA, None, actual[i]))

    return diffs


def _compare_dicts(path: DiffPath, expected: dict, actual: dict) -> list[Diff]:
    """Compare two dicts key by key."""
    diffs: list[Diff] = []
    all_keys = set(expected.keys()) | set(actual.keys())

    for key in sorted(all_keys, key=lambda k: (type(k).__name__, str(k))):
        # Create path segment - use .key for strings, [key] for others
        if isinstance(key, str):
            key_path = path.child(key)
        else:
            key_path = path.child(f"[{key!r}]")

        if key not in actual:
            diffs.append(Diff(key_path, DiffKind.MISSING, expected[key], None))
        elif key not in expected:
            diffs.append(Diff(key_path, DiffKind.EXTRA, None, actual[key]))
        else:
            diffs.extend(compare_values(key_path, expected[key], actual[key]))

    return diffs


def _compare_attrs(path: DiffPath, expected: Any, actual: Any) -> list[Diff]:
    """Compare all declared fields, including reader-populated init=False state."""
    diffs: list[Diff] = []
    for field in attrs.fields(type(expected)):
        before = getattr(expected, field.name, _UNSET)
        after = getattr(actual, field.name, _UNSET)
        field_path = path.child(field.name)
        if before is _UNSET and after is _UNSET:
            continue
        if before is _UNSET:
            diffs.append(Diff(field_path, DiffKind.EXTRA, None, after))
        elif after is _UNSET:
            diffs.append(Diff(field_path, DiffKind.MISSING, before, None))
        else:
            diffs.extend(compare_values(field_path, before, after))
    return diffs


def compare_properties(
    path: DiffPath, expected: list[Property], actual: list[Property]
) -> list[Diff]:
    """Compare serialized property slots in order; inherited names may repeat."""
    diffs: list[Diff] = []
    slots = path.child("properties")
    for index in range(max(len(expected), len(actual))):
        slot_path = slots.child(index)
        if index >= len(actual):
            diffs.append(Diff(slot_path, DiffKind.MISSING, expected[index], None))
        elif index >= len(expected):
            diffs.append(Diff(slot_path, DiffKind.EXTRA, None, actual[index]))
        else:
            diffs.extend(_compare_attrs(slot_path, expected[index], actual[index]))
    return diffs


def compare_verbs(path: DiffPath, expected: list[Verb], actual: list[Verb]) -> list[Diff]:
    """Compare two verb lists."""
    diffs: list[Diff] = []

    # Compare by index since verbs can have duplicate names
    if len(expected) != len(actual):
        diffs.append(Diff(
            path.child("verbs"),
            DiffKind.LENGTH_MISMATCH,
            len(expected),
            len(actual),
        ))

    # Compare verbs up to the shorter length
    for i in range(min(len(expected), len(actual))):
        verb_path = path.child("verbs").child(i)
        exp_verb = expected[i]
        act_verb = actual[i]

        # Compare name
        if exp_verb.name != act_verb.name:
            diffs.append(Diff(
                verb_path.child("name"),
                DiffKind.VALUE_CHANGED,
                exp_verb.name,
                act_verb.name,
            ))

        # Compare owner
        if exp_verb.owner != act_verb.owner:
            diffs.append(Diff(
                verb_path.child("owner"),
                DiffKind.VALUE_CHANGED,
                exp_verb.owner,
                act_verb.owner,
            ))

        # Compare perms
        if exp_verb.perms != act_verb.perms:
            diffs.append(Diff(
                verb_path.child("perms"),
                DiffKind.VALUE_CHANGED,
                exp_verb.perms,
                act_verb.perms,
            ))

        # Compare preps
        if exp_verb.preps != act_verb.preps:
            diffs.append(Diff(
                verb_path.child("preps"),
                DiffKind.VALUE_CHANGED,
                exp_verb.preps,
                act_verb.preps,
            ))

        # Compare code (None vs [] is a difference)
        if exp_verb.code is None and act_verb.code is None:
            pass  # Both None
        elif exp_verb.code is None or act_verb.code is None:
            diffs.append(Diff(
                verb_path.child("code"),
                DiffKind.VALUE_CHANGED,
                exp_verb.code,
                act_verb.code,
            ))
        else:
            diffs.extend(compare_values(verb_path.child("code"), exp_verb.code, act_verb.code))

    # Missing verbs (in expected but not actual)
    for i in range(len(actual), len(expected)):
        diffs.append(Diff(
            path.child("verbs").child(i),
            DiffKind.MISSING,
            expected[i],
            None,
        ))

    # Extra verbs (in actual but not expected)
    for i in range(len(expected), len(actual)):
        diffs.append(Diff(
            path.child("verbs").child(i),
            DiffKind.EXTRA,
            None,
            actual[i],
        ))

    return diffs


def compare_objects(path: DiffPath, expected: MooObject, actual: MooObject) -> list[Diff]:
    """Compare two MooObject instances."""
    diffs: list[Diff] = []

    # Compare scalar fields
    if expected.name != actual.name:
        diffs.append(Diff(path.child("name"), DiffKind.VALUE_CHANGED, expected.name, actual.name))

    if expected.flags != actual.flags:
        diffs.append(Diff(path.child("flags"), DiffKind.VALUE_CHANGED, expected.flags, actual.flags))

    if expected.owner != actual.owner:
        diffs.append(Diff(path.child("owner"), DiffKind.VALUE_CHANGED, expected.owner, actual.owner))

    if expected.location != actual.location:
        diffs.append(Diff(path.child("location"), DiffKind.VALUE_CHANGED, expected.location, actual.location))

    if expected.last_move != actual.last_move:
        diffs.append(Diff(path.child("last_move"), DiffKind.VALUE_CHANGED, expected.last_move, actual.last_move))

    if expected.propdefs_count != actual.propdefs_count:
        diffs.append(Diff(path.child("propdefs_count"), DiffKind.VALUE_CHANGED, expected.propdefs_count, actual.propdefs_count))

    if expected.anon != actual.anon:
        diffs.append(Diff(path.child("anon"), DiffKind.VALUE_CHANGED, expected.anon, actual.anon))

    for field in ("v4_blank_line", "v4_first_content", "v4_neighbor", "v4_first_child", "v4_sibling"):
        diffs.extend(compare_values(path.child(field), getattr(expected, field), getattr(actual, field)))

    # Compare list fields
    diffs.extend(compare_values(path.child("parents"), expected.parents, actual.parents))
    diffs.extend(compare_values(path.child("children"), expected.children, actual.children))
    diffs.extend(compare_values(path.child("contents"), expected.contents, actual.contents))

    # Compare properties
    diffs.extend(compare_properties(path, expected.properties, actual.properties))

    # Compare verbs
    diffs.extend(compare_verbs(path, expected.verbs, actual.verbs))

    return diffs


def compare_waif(path: DiffPath, expected: Waif, actual: Waif) -> list[Diff]:
    """Compare two Waif instances."""
    diffs: list[Diff] = []

    # Compare scalar fields
    if expected.waif_class != actual.waif_class:
        diffs.append(Diff(
            path.child("waif_class"),
            DiffKind.VALUE_CHANGED,
            expected.waif_class,
            actual.waif_class,
        ))

    if expected.owner != actual.owner:
        diffs.append(Diff(
            path.child("owner"),
            DiffKind.VALUE_CHANGED,
            expected.owner,
            actual.owner,
        ))

    if expected.propdefs_length != actual.propdefs_length:
        diffs.append(Diff(
            path.child("propdefs_length"),
            DiffKind.VALUE_CHANGED,
            expected.propdefs_length,
            actual.propdefs_length,
        ))

    # Compare props list (list of (slot_index, value) tuples)
    diffs.extend(compare_values(path.child("props"), expected.props, actual.props))

    return diffs


def compare_waifs(
    path: DiffPath, expected: dict[int, Waif], actual: dict[int, Waif]
) -> list[Diff]:
    """Compare two waif dictionaries."""
    diffs: list[Diff] = []
    all_indices = set(expected.keys()) | set(actual.keys())

    for idx in sorted(all_indices):
        waif_path = path.child("waifs").child(idx)

        if idx not in actual:
            diffs.append(Diff(waif_path, DiffKind.MISSING, expected[idx], None))
        elif idx not in expected:
            diffs.append(Diff(waif_path, DiffKind.EXTRA, None, actual[idx]))
        else:
            diffs.extend(compare_waif(waif_path, expected[idx], actual[idx]))

    return diffs


def compare_databases(
    expected: MooDatabase,
    actual: MooDatabase,
    *,
    ignore_fields: set[str] | None = None,
    max_diffs: int | None = None,
) -> CompareResult:
    """
    Compare two MooDatabase instances and return all differences.

    Args:
        expected: The expected (reference) database
        actual: The actual database to compare
        ignore_fields: Optional set of top-level field names to skip
        max_diffs: Optional limit on number of differences to return

    Returns:
        CompareResult containing all found differences
    """
    ignore = ignore_fields or set()
    diffs: list[Diff] = []
    root = DiffPath.root()

    def _add_diff(diff: Diff) -> bool:
        """Add diff and return True if we should continue."""
        diffs.append(diff)
        return max_diffs is None or len(diffs) < max_diffs

    def _add_diffs(new_diffs: list[Diff]) -> bool:
        """Add diffs and return True if we should continue."""
        for diff in new_diffs:
            if not _add_diff(diff):
                return False
        return True

    # Compare version info
    if "version" not in ignore:
        if expected.version != actual.version:
            if not _add_diff(Diff(root.child("version"), DiffKind.VALUE_CHANGED, expected.version, actual.version)):
                return CompareResult(diffs)

    if "versionstring" not in ignore:
        if expected.versionstring != actual.versionstring:
            if not _add_diff(Diff(root.child("versionstring"), DiffKind.VALUE_CHANGED, expected.versionstring, actual.versionstring)):
                return CompareResult(diffs)

    # Compare total_objects
    if "total_objects" not in ignore:
        if expected.total_objects != actual.total_objects:
            if not _add_diff(Diff(root.child("total_objects"), DiffKind.VALUE_CHANGED, expected.total_objects, actual.total_objects)):
                return CompareResult(diffs)

    # Compare players list
    if "players" not in ignore:
        player_diffs = compare_values(root.child("players"), expected.players, actual.players)
        if not _add_diffs(player_diffs):
            return CompareResult(diffs)

    # Compare recycled_objects (as sorted lists for consistent comparison)
    if "recycled_objects" not in ignore:
        exp_recycled = sorted(expected.recycled_objects)
        act_recycled = sorted(actual.recycled_objects)
        recycled_diffs = compare_values(root.child("recycled_objects"), exp_recycled, act_recycled)
        if not _add_diffs(recycled_diffs):
            return CompareResult(diffs)

    if "pending_values" not in ignore:
        pending_diffs = compare_values(
            root.child("pending_values"),
            expected.pending_values,
            actual.pending_values,
        )
        if not _add_diffs(pending_diffs):
            return CompareResult(diffs)

    for field in (
        "total_verbs", "total_players", "clocks", "queuedTasks", "suspendedTasks", "interruptedTasks",
        "connections", "connections_with_listeners", "has_connections_section", "line_ending", "v4_dummy",
    ):
        if field not in ignore:
            if not _add_diffs(compare_values(root.child(field), getattr(expected, field), getattr(actual, field))):
                return CompareResult(diffs)

    # Compare objects
    if "objects" not in ignore:
        all_obj_ids = set(expected.objects.keys()) | set(actual.objects.keys())
        for obj_id in sorted(all_obj_ids):
            obj_path = DiffPath.object(obj_id)

            if obj_id not in actual.objects:
                if not _add_diff(Diff(obj_path, DiffKind.MISSING, expected.objects[obj_id], None)):
                    return CompareResult(diffs)
            elif obj_id not in expected.objects:
                if not _add_diff(Diff(obj_path, DiffKind.EXTRA, None, actual.objects[obj_id])):
                    return CompareResult(diffs)
            else:
                obj_diffs = compare_objects(obj_path, expected.objects[obj_id], actual.objects[obj_id])
                if not _add_diffs(obj_diffs):
                    return CompareResult(diffs)

    # Compare waifs
    if "waifs" not in ignore:
        waif_diffs = compare_waifs(root, expected.waifs, actual.waifs)
        if not _add_diffs(waif_diffs):
            return CompareResult(diffs)

    return CompareResult(diffs)


# Full reports deliberately keep their pairing/precision policy separate from
# the published legacy adapter above. Both policies live in this equality owner.
import difflib
import hashlib
from collections import Counter, defaultdict
from .database import CLEAR, Anon, MooCatch, MooError, MooFinally, ObjNum
from .diff_types import (Comparison, MISSING, UNSET, POLICY_VERSION, SECTIONS,
                         ReportOptions, canonical_json, encode_value, event_sort_key, finalize_counts, value_kind)
from .map_keys import NUMERIC_MAP_KEY_POLICY, numeric_key_identity


# Every attrs model field is explicitly accounted for, including derived fields.
# Tests compare this registry to the live model to detect reader/model drift.
FIELD_COVERAGE = {
    MooDatabase: {
        'versionstring': 'serialization', 'version': 'serialization', 'total_objects': 'serialization',
        'total_verbs': 'serialization', 'total_players': 'serialization', 'clocks': 'runtime',
        'objects': 'objects', 'queuedTasks': 'runtime', 'suspendedTasks': 'runtime', 'interruptedTasks': 'runtime',
        'waifs': 'heap', 'players': 'players', 'recycled_objects': 'recycled', 'pending_values': 'runtime',
        'connections': 'runtime', 'connections_with_listeners': 'runtime', 'has_connections_section': 'runtime',
        'line_ending': 'serialization', 'v4_dummy': 'serialization',
    },
    MooObject: {
        'id': 'identity', 'name': 'objects', 'flags': 'objects', 'owner': 'objects', 'location': 'objects',
        'parents': 'objects', 'children': 'objects', 'last_move': 'objects', 'contents': 'objects',
        'verbs': 'verbs', 'properties': 'properties', 'propdefs_count': 'properties', 'anon': 'identity',
        'v4_blank_line': 'serialization', 'v4_first_content': 'serialization', 'v4_neighbor': 'serialization',
        'v4_first_child': 'serialization', 'v4_sibling': 'serialization',
    },
    Property: {'propertyName': 'properties', 'value': 'properties', 'owner': 'properties', 'perms': 'properties'},
    Verb: {'name': 'verbs', 'owner': 'verbs', 'perms': 'verbs', 'preps': 'verbs', 'object': 'derived-invariant', 'code': 'verbs'},
    Waif: {'waif_class': 'heap', 'owner': 'heap', 'props': 'heap', 'propdefs_length': 'heap'},
    Activation: {name: 'runtime' for name in (
        'this', 'threaded', 'player', 'programmer', 'vloc', 'debug', 'verb', 'verbname', 'code', 'stack',
        'unused1', 'unused2', 'unused3', 'unused4', 'temp_value', 'temp_this', 'temp_vloc', 'rtEnv', 'temp_end',
        'pc', 'bi_func', 'error', 'bi_func_name', 'bi_func_data', 'language_version', 'argstr', 'dobjstr', 'prepstr', 'iobjstr')},
    VM: {name: 'runtime' for name in ('locals', 'stack', 'top', 'vector', 'funcId', 'maxStackframes')},
    QueuedTask: {name: 'runtime' for name in ('firstLineno', 'id', 'st', 'unused', 'value', 'activation', 'rtEnv', 'code')},
    SuspendedTask: {name: 'runtime' for name in ('firstLineno', 'id', 'startTime', 'value', 'vm')},
    InterruptedTask: {name: 'runtime' for name in ('id', 'status', 'vm')},
    WaifReference: {'index': 'heap-reference'},
}


def verify_field_coverage():
    for cls, fields in FIELD_COVERAGE.items():
        actual = {f.name for f in attrs.fields(cls)}
        if actual != set(fields):
            raise ValueError(f'undeclared comparison coverage for {cls.__name__}: {sorted(actual ^ set(fields))}')


def _field(name):
    return {'type': 'field', 'name': name}


def _index(index):
    return {'type': 'index', 'index': str(index)}


def _reference(value):
    if isinstance(value, WaifReference):
        return 'waif', value.index
    if isinstance(value, Anon) and int(value) != -1:
        return 'anonymous', int(value)
    return None


def _contains_heap(value):
    if _reference(value):
        return True
    if isinstance(value, (list, tuple)):
        return any(_contains_heap(v) for v in value)
    if isinstance(value, dict):
        return any(_contains_heap(k) or _contains_heap(v) for k, v in value.items())
    if attrs.has(type(value)):
        return any(_contains_heap(getattr(value, f.name, UNSET)) for f in attrs.fields(type(value)))
    return False


def _scalar(value):
    return (not isinstance(value, (list, tuple, dict, set)) and not attrs.has(type(value))) or isinstance(value, (ObjNum, Anon, MooError, MooCatch, MooFinally, WaifReference))


class _Stopped(Exception):
    pass


class _DiscoveryPath:
    """Interned immutable prefix; a proposal retains one pointer per path."""
    __slots__ = ('parent', 'step', 'encoded', 'depth', 'pool', 'base')

    def __init__(self, pool, parent=None, step=None, encoded=None):
        self.pool, self.parent, self.step, self.encoded = pool, parent, step, encoded
        self.depth = 0 if parent is None else parent.depth + 1
        self.base = () if parent is None else parent.base

    def __add__(self, steps):
        return self.pool.extend(self, steps)

    def nodes(self):
        path, current = [], self
        while current.parent is not None:
            path.append(current)
            current = current.parent
        return reversed(path)

    def __iter__(self):
        return (node.step for node in self.nodes())

    def compare(self, other):
        if self is other:
            return 0
        # Most candidates belong to different selected property roots. Their
        # distinct first step decides order without walking heap ancestors.
        for left, right in zip(self.base, other.base):
            if left != right:
                return -1 if left < right else 1
        left, right = iter(self.nodes()), iter(other.nodes())
        while True:
            a, b = next(left, None), next(right, None)
            if a is None or b is None:
                if a is b:
                    return 0
                # The comma preceding another step sorts before the closing
                # bracket, exactly as canonical JSON arrays do.
                return 1 if a is None else -1
            if a.encoded != b.encoded:
                return -1 if a.encoded < b.encoded else 1


class _DiscoveryPaths:
    __slots__ = ('database', 'empty', 'prefixes', 'steps', 'certificates')

    def __init__(self, database):
        self.database = database
        self.empty = _DiscoveryPath(self)
        self.prefixes = {}
        self.steps = {}
        self.certificates = {}

    def extend(self, parent, steps):
        for step in steps:
            raw = canonical_json(step)
            certificate = None
            if step['type'] == 'map-key' and step['key']['type'] in ('waif', 'anon'):
                kind = 'waif' if step['key']['type'] == 'waif' else 'anonymous'
                number = int(step['key']['value'])
                if kind != 'anonymous' or number != -1:
                    certificate = self.certificates.get((kind, number))
                    if certificate is None:
                        raise ValueError('reference map key has no established rooted certificate')
            # Certificate identity participates in interning too: a provisional
            # mapping may change while the raw key spelling remains the same.
            token = raw if certificate is None else (raw, certificate)
            stored = self.steps.get(token)
            if stored is None:
                cleaned = dict(step)
                if step['type'] == 'heap':
                    for field in ('raw_id', 'old_id', 'new_id', 'id'):
                        cleaned.pop(field, None)
                if step['type'] == 'property':
                    definer = self.database.objects.get(int(step['definer']))
                    if definer is not None and definer.anon:
                        cleaned['definer'] = '@anonymous'
                if certificate is not None:
                    cleaned['key'] = {'type': step['key']['type'], 'certificate': certificate}
                stored = (token, step, canonical_json(cleaned))
                self.steps[token] = stored
            key = (parent, stored[0])
            child = self.prefixes.get(key)
            if child is None:
                child = _DiscoveryPath(self, parent, stored[1], stored[2])
                self.prefixes[key] = child
            parent = child
        return parent

    def root(self, steps):
        path = self.extend(self.empty, steps)
        path.base = tuple(node.encoded for node in path.nodes())
        return path

    def retain(self, paths):
        needed = set()
        for path in paths:
            while path.parent is not None and path not in needed:
                needed.add(path)
                path = path.parent
        self.prefixes = {key: path for key, path in self.prefixes.items() if path in needed}
        used_steps = {key[1] for key in self.prefixes}
        self.steps = {key: value for key, value in self.steps.items() if key in used_steps}


def _discovery_evidence_order(left, right):
    a, b = canonical_json(left['entity']), canonical_json(right['entity'])
    if a != b:
        return -1 if a < b else 1
    for side in ('new_path', 'old_path'):
        difference = left[side].compare(right[side])
        if difference:
            return difference
    return 0


def _discovery_evidence_digest(evidence):
    digest = hashlib.sha256()
    digest.update(b'{"entity":' + canonical_json(evidence['entity']) + b',"new_path":[')
    for side in ('new_path', 'old_path'):
        if side == 'old_path':
            digest.update(b',"old_path":[')
        for index, node in enumerate(evidence[side].nodes()):
            if index:
                digest.update(b',')
            digest.update(node.encoded)
        digest.update(b']')
    digest.update(b'}')
    return digest.hexdigest()


def _discovery_candidate(a, b):
    ar, br = _reference(a), _reference(b)
    if ar or br:
        return ar is not None and br is not None and ar[0] == br[0]
    return type(a) is type(b) and (isinstance(a, (list, tuple, dict)) or attrs.has(type(a)) and not _scalar(a))


class _SnapshotComparison:
    def __init__(self, old, new, options):
        self.old, self.new, self.options = old, new, options
        self.sections = set(options.sections)
        self.events = []
        self.errors = []
        self.identity_complete = True
        self.scan_complete = True
        self.forward = defaultdict(set)
        self.reverse = defaultdict(set)
        self.proposals = defaultdict(list)
        self.pair_roots = defaultdict(dict)
        self.pairs = set()
        self.dependencies = defaultdict(set)
        self.blocked = set()
        self.blocked_old = set()
        self.blocked_new = set()
        self.anchors = defaultdict(list)
        self.heap_ids = {}
        self.roots = []
        self.discovery_seen = set()
        self.proposal_seen = defaultdict(set)
        self.discovery_paths = {'old': _DiscoveryPaths(old), 'new': _DiscoveryPaths(new)}
        self.discovery_frontier = None
        self.uncertain_keys = {'old': set(), 'new': set()}
        self.discovery_edges = {}
        self.key_certificates = {}
        self.layouts = {}
        self.reached = {'old': set(), 'new': set()}
        permanent = {n for database in (old, new) for n, o in database.objects.items() if not o.anon}
        if options.object_ids is not None:
            for n in options.object_ids:
                if n not in permanent and n not in old.recycled_objects and n not in new.recycled_objects:
                    raise ValueError(f'object #{n} is absent from both sources')
            permanent = set(options.object_ids)
        self.ids = sorted(permanent | (set(old.recycled_objects) | set(new.recycled_objects) if options.object_ids is None else set()))

    def error(self, message, **details):
        item = {'message': message, **details}
        if item not in self.errors:
            self.errors.append(item)

    def evidence(self, value, database, selected=None):
        """Encode complete evidence for the selected stored-field scope only."""
        selected = self.sections if selected is None else set(selected)

        def restrict(encoded, heap_body=False):
            if not isinstance(encoded, dict):
                return encoded
            if encoded.get('type') == 'graph':
                root = restrict(encoded['root'], heap_body)
                if 'heap' not in selected:
                    return root
                return {'type': 'graph', 'root': root,
                        'nodes': [{**node, 'value': restrict(node['value'], True)} for node in encoded['nodes']]}
            if encoded.get('type') == 'record' and encoded.get('record_type') == 'MooObject':
                fields = {}
                for field, child in encoded['fields'].items():
                    section = FIELD_COVERAGE[MooObject][field]
                    include = section == 'identity' or section in selected
                    if heap_body:
                        include = section != 'serialization' or self.options.view == 'checkpoint'
                    if include:
                        fields[field] = restrict(child, heap_body)
                return {**encoded, 'fields': fields}
            if encoded.get('type') == 'record' and encoded.get('record_type') == 'Verb':
                return {**encoded, 'fields': {field: restrict(child, heap_body) for field, child in encoded['fields'].items() if field != 'object'}}
            return {field: [restrict(child, heap_body) for child in item] if isinstance(item, list)
                           else restrict(item, heap_body) if isinstance(item, dict) else item
                    for field, item in encoded.items()}

        # A bare anonymous lifecycle record is itself heap evidence, just as
        # an anonymous node inside a graph table is. Heap owns all its fields.
        return restrict(encode_value(value, database), isinstance(value, MooObject) and value.anon and 'heap' in selected)

    def emit(self, entity, category, section, old_path, new_path, old, new, kind='changed', definite=True, details=None, applicable=None, evidence_sections=None):
        applicable = applicable or (section,)
        if not self.sections.intersection(applicable):
            return
        event = {'record': 'event', 'category': category, 'section': section, 'kind': kind,
                 'definite_change': definite, 'entity': entity, 'old_path': old_path, 'new_path': new_path,
                 'old': self.evidence(old, self.old, evidence_sections), 'new': self.evidence(new, self.new, evidence_sections),
                 'labels': {'old': None, 'new': None}, 'details': details or {}}
        if entity['type'] == 'permanent':
            n = int(entity['id'])
            event['labels'] = {'old': self.old.name_of(n), 'new': self.new.name_of(n)}
        if tuple(applicable) != (section,):
            event['sections_applicable'] = list(applicable)
        if entity['type'] in ('anonymous', 'waif'):
            event['details'].setdefault('anchors', self.anchors.get(entity['id'], []))
        self.events.append(event)
        if kind in ('identity_conflict', 'identity_unresolved'):
            self.identity_complete = False
        if self.options.stop_after is not None and len(self.events) >= self.options.stop_after:
            self.scan_complete = False
            raise _Stopped()

    def key(self, key, side, correspondence=True):
        if not isinstance(key, (int, bool, float, str, ObjNum, MooError, Anon, WaifReference)):
            raise ValueError('unsupported server map key type')
        if isinstance(key, str) and '\x00' in key:
            raise ValueError('NUL-containing map key has unsupported server equality')
        if type(key) in (int, ObjNum, MooError):
            # This configured GNU comparator narrows numeric subtraction to
            # C int. The shared policy checks wire widths and retains type
            # namespaces; numeric VALUES still use full parsed precision.
            return encode_value(key)['type'], numeric_key_identity(key)
        ref = _reference(key)
        if ref:
            if self.options.view == 'checkpoint' or not correspondence:
                return ref
            if ref in self.uncertain_keys[side]:
                return None
            if self.discovery_frontier is not None:
                return self.discovery_frontier[side].get(ref)
            if side == 'old':
                matches = self.forward.get(ref, set())
                if len(matches) != 1 or ref in self.blocked_old:
                    return None
                target = next(iter(matches))
                if len(self.reverse.get(target, set())) != 1 or target in self.blocked_new:
                    return None
                return target
            if len(self.reverse.get(ref, set())) != 1 or ref in self.blocked_new:
                return None
            return ref
        if isinstance(key, float):
            if math.isnan(key):
                raise ValueError('NaN map key has unsupported server equality')
            # Map keys follow server equality; float values still compare bits.
            return ('float', 0.0 if key == 0.0 else key)
        if isinstance(key, str):
            # Server strcasecmp is byte folding, not Unicode casefold.
            return ('string', key.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')))
        if isinstance(key, (list, dict, set)):
            raise ValueError('unsupported compound map key')
        encoded = encode_value(key)
        return encoded['type'], canonical_json(encoded)

    def map_pairs(self, a, b):
        before, after = {}, {}
        unresolved = False
        for side, values, keyed in (('old', a, before), ('new', b, after)):
            for k, v in values.items():
                token = self.key(k, side)
                if token is None:
                    unresolved = True
                    continue
                if token in keyed:
                    raise ValueError('duplicate map keys under server equality')
                keyed[token] = (k, v)
        return before, after, unresolved

    def value(self, a, b, entity, category, section, op, np, details=None):
        if section not in self.sections:
            return
        if a is MISSING or b is MISSING:
            self.emit(entity, category, section, op, np, a, b, 'added' if a is MISSING else 'removed', details=details)
            return
        ar, br = _reference(a), _reference(b)
        if self.options.view != 'checkpoint' and (ar or br):
            if ar and br and ar[0] == br[0]:
                if ar in self.blocked_old or br in self.blocked_new:
                    return  # The complete conflicting component has its own evidence.
                if self.forward.get(ar) == {br} and self.reverse.get(br) == {ar}:
                    return
                self.emit(entity, category, section, op, np, a, b, 'identity_unresolved', False, details)
                return
        if type(a) is not type(b):
            # Flags and normal integer storage share the integer encoding.
            if encode_value(a) == encode_value(b):
                return
            self.emit(entity, category, section, op, np, a, b, 'type_changed', details=details)
            return
        # Equal scalar encodings need no intermediate JSON allocation. Float
        # equality must remain exact for signed zero and available NaN bits.
        if _scalar(a) and _scalar(b):
            if isinstance(a, float):
                if struct.pack('>d', a) == struct.pack('>d', b):
                    return
            elif a == b:
                return
        if isinstance(a, (list, tuple)):
            if len(a) == len(b):
                for i, (x, y) in enumerate(zip(a, b)):
                    self.value(x, y, entity, category, section, op + [_index(i)], np + [_index(i)], details)
            elif all(_scalar(v) and not _reference(v) for v in (*a, *b)):
                matcher = difflib.SequenceMatcher(None, [canonical_json(encode_value(v)) for v in a],
                                                 [canonical_json(encode_value(v)) for v in b], autojunk=False)
                opcodes = [{'tag': tag, 'old_range': [i, j], 'new_range': [k, l],
                            'old': [encode_value(v) for v in a[i:j]], 'new': [encode_value(v) for v in b[k:l]]}
                           for tag, i, j, k, l in matcher.get_opcodes() if tag != 'equal']
                self.emit(entity, category, section, op, np, a, b, details={**(details or {}), 'opcodes': opcodes})
            else:
                self.emit(entity, category, section, op, np, a, b, details=details)
                if self.options.view != 'checkpoint' and (_contains_heap(a) or _contains_heap(b)):
                    self.emit(entity, category, section, op, np, a, b, 'identity_unresolved', False,
                              {**(details or {}), 'reason': 'unequal structured collection length'})
            return
        if isinstance(a, dict):
            before, after, unresolved = self.map_pairs(a, b)
            if unresolved and len(a) != len(b):
                self.emit(entity, category, section, op, np, a, b,
                          details={**(details or {}), 'reason': 'unequal map entry count in unresolved region'})
                self.emit(entity, category, section, op, np, a, b, 'identity_unresolved', False,
                          {**(details or {}), 'reason': 'unanchored reference map keys'})
                return
            for token in sorted(before.keys() | after.keys(), key=repr):
                ak, av = before.get(token, (MISSING, MISSING))
                bk, bv = after.get(token, (MISSING, MISSING))
                ap = op + [{'type': 'map-key', 'key': encode_value(ak)}]
                bp = np + [{'type': 'map-key', 'key': encode_value(bk)}]
                if self.options.view == 'checkpoint' and ak is not MISSING and bk is not MISSING:
                    self.value(ak, bk, entity, category, section, ap + [_field('key')], bp + [_field('key')], details)
                self.value(av, bv, entity, category, section, ap, bp, details)
            if self.options.view == 'checkpoint':
                old_order = [self.key(k, 'old') for k in a]
                new_order = [self.key(k, 'new') for k in b]
                if Counter(old_order) == Counter(new_order) and old_order != new_order:
                    self.emit(entity, category, section, op, np, list(a), list(b), 'order_changed', details=details)
            if unresolved:
                self.emit(entity, category, section, op, np, a, b, 'identity_unresolved', False,
                          {**(details or {}), 'reason': 'unanchored reference map keys'})
            return
        if attrs.has(type(a)) and not _scalar(a):
            for f in attrs.fields(type(a)):
                self.value(getattr(a, f.name, UNSET), getattr(b, f.name, UNSET), entity, category, section,
                           op + [_field(f.name)], np + [_field(f.name)], details)
            return
        if canonical_json(encode_value(a)) != canonical_json(encode_value(b)):
            self.emit(entity, category, section, op, np, a, b, details=details)

    def layout(self, database, obj):
        cache_key = (id(database), obj.id)
        if cache_key in self.layouts:
            return self.layouts[cache_key]
        result = []
        valid = True
        ancestors = database.ancestors(obj)
        for ancestor in ancestors:
            if any(int(p) != -1 and int(p) not in database.objects for p in ancestor.parents):
                valid = False
            if ancestor.propdefs_count < 0 or ancestor.propdefs_count > len(ancestor.properties):
                valid = False
            occurrences = Counter()
            for pos, prop in enumerate(ancestor.properties[:ancestor.propdefs_count]):
                name = prop.propertyName
                if not isinstance(name, str):
                    valid = False
                    name = str(name)
                key = (ancestor.id, name, occurrences[name])
                occurrences[name] += 1
                result.append((key, {'type': 'property', 'slot': str(len(result)), 'definer': str(ancestor.id),
                                     'name': name, 'occurrence': str(key[2]), 'definition_position': str(pos)}))
        if len(result) != len(obj.properties) or len({k for k, _ in result}) != len(result):
            valid = False
        self.layouts[cache_key] = result if valid else None
        return self.layouts[cache_key]

    def property_pairs(self, a, b):
        la, lb = self.layout(self.old, a), self.layout(self.new, b)
        if self.options.view == 'checkpoint' or la is None or lb is None:
            return [(i, a.properties[i] if i < len(a.properties) else MISSING,
                     b.properties[i] if i < len(b.properties) else MISSING,
                     [_field('properties'), _index(i)], [_field('properties'), _index(i)])
                    for i in range(max(len(a.properties), len(b.properties)))], la, lb
        def definition_key(key, obj):
            # An anonymous node's own definer number is dump-local, just like
            # the node itself. Pair own definitions under the established pair.
            return (-1, key[1], key[2]) if obj.anon and key[0] == obj.id else key
        before = {definition_key(key, a): (a.properties[i], step) for i, (key, step) in enumerate(la)}
        after = {definition_key(key, b): (b.properties[i], step) for i, (key, step) in enumerate(lb)}
        pairs = []
        for key in sorted(before.keys() | after.keys()):
            ap, astep = before.get(key, (MISSING, None))
            bp, bstep = after.get(key, (MISSING, None))
            pairs.append((key, ap, bp, [astep] if astep else [], [bstep] if bstep else []))
        return pairs, la, lb

    def prepare_roots(self):
        for n in self.ids:
            a, b = self.old.objects.get(n), self.new.objects.get(n)
            if a is not None and a.anon:
                a = None
            if b is not None and b.anon:
                b = None
            if a is not None and b is not None:
                pairs, _, _ = self.property_pairs(a, b)
                for _, ap, bp, op, np in pairs:
                    av = ap.value if ap is not MISSING else MISSING
                    bv = bp.value if bp is not MISSING else MISSING
                    self.roots.append((av, bv, {'type': 'permanent', 'id': str(n)}, op + [_field('value')], np + [_field('value')]))
            else:
                for side, o in (('old', a), ('new', b)):
                    if o:
                        for i, p in enumerate(o.properties):
                            path = [_field('properties'), _index(i), _field('value')]
                            self.roots.append((p.value if side == 'old' else MISSING, p.value if side == 'new' else MISSING,
                                               {'type': 'permanent', 'id': str(n)}, path, path))

    def node(self, ref, side):
        database = self.old if side == 'old' else self.new
        kind, number = ref
        value = database.waifs.get(number) if kind == 'waif' else database.objects.get(number)
        if value is None or kind == 'anonymous' and not value.anon:
            self.error('dangling heap reference', side=side, kind=kind, id=str(number))
            return MISSING
        return value

    def reach(self, value, side):
        pending = [value]
        containers = set()
        while pending:
            current = pending.pop()
            ref = _reference(current)
            if ref:
                if ref not in self.reached[side]:
                    self.reached[side].add(ref)
                    pending.append(self.node(ref, side))
                continue
            if isinstance(current, (list, tuple, dict)) or attrs.has(type(current)) and not _scalar(current):
                identity = id(current)
                if identity in containers:
                    continue
                containers.add(identity)
            if isinstance(current, (list, tuple)):
                pending.extend(reversed(current))
            elif isinstance(current, dict):
                children = [child for key, child in current.items() for child in (key, child)]
                pending.extend(reversed(children))
            elif attrs.has(type(current)) and not _scalar(current):
                pending.extend(getattr(current, f.name, UNSET) for f in reversed(attrs.fields(type(current))) if f.name != 'code')

    def discover(self, a, b, anchor, op, np, parent=None):
        if not _discovery_candidate(a, b):
            return
        root = {'entity': anchor, 'old_path': op, 'new_path': np}
        root_key = canonical_json(root)
        pending = [(a, b, self.discovery_paths['old'].root(op),
                    self.discovery_paths['new'].root(np), () if parent is None else (parent,))]
        while pending:
            av, bv, old_path, new_path, parent_pairs = pending.pop()
            ar, br = _reference(av), _reference(bv)
            children = []
            if ar and br and ar[0] == br[0]:
                self.forward[ar].add(br); self.reverse[br].add(ar)
                pair = (ar, br)
                evidence = {'entity': anchor, 'old_path': old_path, 'new_path': new_path}
                token = (root_key, old_path, new_path)
                if token not in self.proposal_seen[pair]:
                    self.proposal_seen[pair].add(token)
                    self.proposals[pair].append(evidence)
                self.pair_roots[pair][root_key] = root
                for parent_pair in parent_pairs:
                    self.dependencies[parent_pair].add(pair)
                self.pairs.add(pair)
                if pair not in self.discovery_seen:
                    self.discovery_seen.add(pair)
                    node_a, node_b = self.node(ar, 'old'), self.node(br, 'new')
                    if node_a is not MISSING and node_b is not MISSING:
                        children = self.node_discovery_edges(node_a, node_b, old_path, new_path, pair)
            elif type(av) is not type(bv):
                continue
            elif isinstance(av, (list, tuple)) and len(av) == len(bv):
                children = [(x, y, old_path + [_index(i)], new_path + [_index(i)], parent_pairs)
                            for i, (x, y) in enumerate(zip(av, bv)) if _discovery_candidate(x, y)]
            elif isinstance(av, dict):
                before, after, _ = self.map_pairs(av, bv)
                for key in sorted(before.keys() & after.keys(), key=repr):
                    ak, x = before[key]; bk, y = after[key]
                    if not _discovery_candidate(x, y):
                        continue
                    key_pair = (_reference(ak), _reference(bk))
                    prerequisites = parent_pairs + ((key_pair,) if all(key_pair) else ())
                    children.append((x, y, old_path + [{'type': 'map-key', 'key': encode_value(ak)}],
                                     new_path + [{'type': 'map-key', 'key': encode_value(bk)}], prerequisites))
            elif attrs.has(type(av)) and not _scalar(av):
                children = [(getattr(av, f.name, UNSET), getattr(bv, f.name, UNSET),
                             old_path + [_field(f.name)], new_path + [_field(f.name)], parent_pairs)
                            for f in attrs.fields(type(av)) if f.name not in ('id', 'object', 'code')
                            and _discovery_candidate(getattr(av, f.name, UNSET), getattr(bv, f.name, UNSET))]
            children.sort(key=cmp_to_key(lambda left, right: left[3].compare(right[3]) or left[2].compare(right[2])))
            pending.extend(reversed(children))

    def node_discovery_edges(self, a, b, op, np, pair):
        if pair not in self.discovery_edges:
            self.discovery_edges[pair] = self.node_discovery_templates(a, b, pair)
        return [(av, bv, op + old_steps, np + new_steps, (pair,))
                for av, bv, old_steps, new_steps in self.discovery_edges[pair]]

    def node_discovery_templates(self, a, b, pair):
        edges = []
        if isinstance(a, Waif):
            before, after = dict(a.props), dict(b.props)
            for slot in sorted(before.keys() & after.keys()):
                if not _discovery_candidate(before[slot], after[slot]):
                    continue
                old_step = {'type': 'heap', 'kind': 'waif', 'old_id': str(pair[0][1]), 'new_id': str(pair[1][1]),
                            'class': str(a.waif_class), 'slot': str(slot)}
                new_step = {**old_step, 'class': str(b.waif_class)}
                edges.append((before[slot], after[slot], [old_step], [new_step]))
        else:
            pairs, _, _ = self.property_pairs(a, b)
            for _, ap, bp, aps, bps in pairs:
                if ap is not MISSING and bp is not MISSING and _discovery_candidate(ap.value, bp.value):
                    edges.append((ap.value, bp.value, aps + [_field('value')], bps + [_field('value')]))
        return edges

    def correspondence(self):
        self.prepare_roots()
        for a, b, anchor, op, np in self.roots:
            self.reach(a, 'old'); self.reach(b, 'new')
        # Every pass reads one immutable, complete key frontier. Rebuild its
        # proposal graph so invalid provisional key mappings cannot survive a
        # later conflict. Only actual roots/local edges retain dependencies.
        frontier = frozenset()
        history = []
        while True:
            self.forward.clear(); self.reverse.clear(); self.proposals.clear()
            self.pair_roots.clear(); self.pairs.clear(); self.dependencies.clear()
            self.proposal_seen.clear()
            self.discovery_frontier = {'old': {ar: br for ar, br in frontier},
                                       'new': {br: br for ar, br in frontier}}
            for side, index in (('old', 0), ('new', 1)):
                self.discovery_paths[side].certificates = {pair[index]: self.key_certificates[pair] for pair in frontier}
            for a, b, anchor, op, np in self.roots:
                self.discovery_seen = set()
                self.discover(a, b, anchor, op, np)
            for side, pool in self.discovery_paths.items():
                pool.retain(e[f'{side}_path'] for rows in self.proposals.values() for e in rows)
            blocked_old, blocked_new = self.blocked_references()
            following = frozenset(pair for pair in self.pairs
                                  if pair[0] not in blocked_old and pair[1] not in blocked_new
                                  and pair[0] not in self.uncertain_keys['old']
                                  and pair[1] not in self.uncertain_keys['new'])
            # A finite certificate is frozen at the pair's first complete
            # bijective frontier. Its establishment evidence uses only earlier
            # frontier certificates, never recursively computed final heap IDs.
            for pair in following - self.key_certificates.keys():
                least = min(self.proposals[pair], key=cmp_to_key(_discovery_evidence_order))
                self.key_certificates[pair] = _discovery_evidence_digest(least)
            if following == frontier:
                break
            history.append(frontier)
            if following in history:
                cycle = history[history.index(following):]
                unstable = set().union(*cycle) - set.intersection(*(set(row) for row in cycle))
                for ar, br in unstable:
                    self.uncertain_keys['old'].add(ar)
                    self.uncertain_keys['new'].add(br)
                self.identity_complete = False
                frontier, history = frozenset(), []
            else:
                frontier = following
        self.discovery_frontier = None
        self.blocked_old, self.blocked_new = self.blocked_references()
        self.blocked = self.blocked_old | self.blocked_new
        # Construction order does not enter IDs: each derives from its least
        # normalized path. Hash only that selected path, using cached step
        # bytes; no complete ancestor arrays/bytes survive per proposal.
        for pair in sorted(self.pairs):
            least = min(self.proposals[pair], key=cmp_to_key(_discovery_evidence_order))
            report_id = 'H' + _discovery_evidence_digest(least)
            self.heap_ids[pair] = report_id
            self.anchors[report_id] = [self.pair_roots[pair][key] for key in sorted(self.pair_roots[pair])]

    def blocked_references(self):
        """Propagate actual non-bijective proposals through their prerequisites."""
        blocked_old = {r for r, matches in self.forward.items() if len(matches) != 1}
        blocked_new = {r for r, matches in self.reverse.items() if len(matches) != 1}
        changed = True
        while changed:
            changed = False
            for pair in self.pairs:
                if pair[0] in blocked_old or pair[1] in blocked_new:
                    for ref, blocked in zip(pair, (blocked_old, blocked_new)):
                        if ref not in blocked:
                            blocked.add(ref); changed = True
                    for child in self.dependencies[pair]:
                        for ref, blocked in zip(child, (blocked_old, blocked_new)):
                            if ref not in blocked:
                                blocked.add(ref); changed = True
        return blocked_old, blocked_new

    def properties(self, a, b, entity, section='properties', category=None):
        if section not in self.sections:
            return
        pairs, la, lb = self.property_pairs(a, b)
        heap = section == 'heap'
        definition_category, value_category = ('heap', 'heap') if heap else ('definitions', 'values')
        if self.options.view != 'checkpoint' and (la is None or lb is None):
            self.emit(entity, definition_category, section, [_field('properties')], [_field('properties')],
                      a.properties, b.properties, 'identity_unresolved', False, {'reason': 'raw-slot provenance fallback'})
        if self.options.view != 'checkpoint' and la is not None and lb is not None:
            keys_a = [(-1, k[1], k[2]) if a.anon and k[0] == a.id else k for k, _ in la]
            keys_b = [(-1, k[1], k[2]) if b.anon and k[0] == b.id else k for k, _ in lb]
            survivors = set(keys_a) & set(keys_b)
            if [k for k in keys_a if k in survivors] != [k for k in keys_b if k in survivors]:
                self.emit(entity, definition_category, section, [_field('property_layout')], [_field('property_layout')],
                          [s for _, s in la], [s for _, s in lb], 'order_changed', details={'subtype': 'property_layout'})
        for key, ap, bp, op, np in pairs:
            if ap is MISSING or bp is MISSING:
                if self.options.view == 'checkpoint' or la is None or lb is None:
                    self.emit(entity, value_category, section, op, np, ap, bp, 'added' if ap is MISSING else 'removed')
                    continue
                definer = key[0]
                is_own_definition = definer == (-1 if a.anon or b.anon else int(entity['id']))
                if is_own_definition:
                    database = self.new if ap is MISSING else self.old
                    count = sum(1 for n in self.ids if n != definer and n in database.objects and
                                any(o.id == definer for o in database.ancestors(database.objects[n])))
                    self.emit(entity, definition_category, section, op, np, ap, bp, 'added' if ap is MISSING else 'removed',
                              details={'subtype': 'property_definition', 'affected_descendants': count})
                else:
                    present = bp if ap is MISSING else ap
                    if present.value is not CLEAR:
                        self.emit(entity, value_category, section, op, np, ap, bp, 'added' if ap is MISSING else 'removed')
                continue
            fields = ('propertyName', 'value', 'owner', 'perms') if self.options.view == 'checkpoint' or la is None or lb is None else ('value', 'owner', 'perms')
            for field in fields:
                field_category = definition_category if field == 'propertyName' else value_category
                self.value(getattr(ap, field, UNSET), getattr(bp, field, UNSET), entity, field_category, section,
                           op + [_field(field)], np + [_field(field)], {'subtype': 'stored_property'})
        if self.options.view == 'checkpoint':
            self.value(a.propdefs_count, b.propdefs_count, entity, definition_category, section,
                       [_field('propdefs_count')], [_field('propdefs_count')])

    def verbs(self, a, b, entity, section='verbs'):
        from .inspection import verb_signature
        category = 'heap' if section == 'heap' else 'code'
        def keyed(o):
            seen = Counter(); result = {}
            for i, v in enumerate(o.verbs):
                signature = verb_signature(v)
                base = signature if self.options.view != 'checkpoint' else (i,)
                key = (*base, seen[base]); seen[base] += 1
                step = {'type': 'verb', 'slot': str(i), 'name': v.name, 'occurrence': str(key[-1]),
                        'signature': {'name': signature[0], 'dobj': str(signature[1]), 'prep': str(signature[2]), 'iobj': str(signature[3])}}
                result[key] = (v, step)
            return result
        before, after = keyed(a), keyed(b)
        survivors = before.keys() & after.keys()
        if self.options.view != 'checkpoint' and [k for k in before if k in survivors] != [k for k in after if k in survivors]:
            self.emit(entity, category, section, [_field('verb_order')], [_field('verb_order')],
                      [s for _, s in before.values()], [s for _, s in after.values()], 'order_changed', details={'subtype': 'verb_order'})
        for key in sorted(before.keys() | after.keys()):
            av, astep = before.get(key, (MISSING, None)); bv, bstep = after.get(key, (MISSING, None))
            op, np = [astep] if astep else [], [bstep] if bstep else []
            if av is MISSING or bv is MISSING:
                source = bv.code if av is MISSING else av.code
                self.emit(entity, category, section, op, np, av, bv, 'added' if av is MISSING else 'removed',
                          details={'subtype': 'verb_definition', 'added_lines': len(source or []) if av is MISSING else 0,
                                   'removed_lines': len(source or []) if bv is MISSING else 0})
                continue
            for field in ('name', 'owner', 'perms', 'preps'):
                self.value(getattr(av, field), getattr(bv, field), entity, category, section,
                           op + [_field(field)], np + [_field(field)], {'subtype': 'verb_metadata'})
            if av.code != bv.code:
                matcher = difflib.SequenceMatcher(None, av.code or [], bv.code or [], autojunk=False)
                hunks = []
                for group in matcher.get_grouped_opcodes(self.options.context):
                    hunks.append({'old_range': [group[0][1] + 1, group[-1][2] + 1],
                                  'new_range': [group[0][3] + 1, group[-1][4] + 1],
                                  'opcodes': [{'tag': t, 'old_range': [i + 1, j + 1], 'new_range': [k + 1, l + 1],
                                               'old_lines': (av.code or [])[i:j], 'new_lines': (bv.code or [])[k:l]}
                                              for t, i, j, k, l in group]})
                codes = matcher.get_opcodes()
                self.emit(entity, category, section, op + [_field('code')], np + [_field('code')], av.code, bv.code,
                          details={'subtype': 'source', 'hunks': hunks,
                                   'added_lines': sum(l-k for t, i, j, k, l in codes if t != 'equal'),
                                   'removed_lines': sum(j-i for t, i, j, k, l in codes if t != 'equal')})

    def object_fields(self, a, b, entity, heap=False):
        for field in ('name', 'flags', 'owner', 'location', 'parents', 'children', 'last_move', 'contents'):
            category = 'definitions' if field in ('name', 'flags', 'owner', 'parents', 'children') else 'values'
            av, bv = getattr(a, field, UNSET), getattr(b, field, UNSET)
            if field in ('parents', 'children') and isinstance(av, list) and isinstance(bv, list) and av != bv and Counter(av) == Counter(bv):
                self.emit(entity, 'heap' if heap else category, 'heap' if heap else 'objects', [_field(field)], [_field(field)], av, bv,
                          'order_changed', details={'subtype': 'object_field'})
                continue
            self.value(av, bv, entity, 'heap' if heap else category,
                       'heap' if heap else 'objects', [_field(field)], [_field(field)], {'subtype': 'object_field'})
        if self.options.view == 'checkpoint':
            for field in ('v4_blank_line', 'v4_first_content', 'v4_neighbor', 'v4_first_child', 'v4_sibling'):
                self.value(getattr(a, field, UNSET), getattr(b, field, UNSET), entity,
                           'heap' if heap else 'serialization', 'heap' if heap else 'serialization', [_field(field)], [_field(field)])

    def permanent(self):
        suppressed = set()
        for n in self.ids:
            a, b = self.old.objects.get(n), self.new.objects.get(n)
            if a is not None and a.anon:
                a = None
            if b is not None and b.anon:
                b = None
            sa = 'present' if a is not None else 'recycled' if n in self.old.recycled_objects else 'absent'
            sb = 'present' if b is not None else 'recycled' if n in self.new.recycled_objects else 'absent'
            entity = {'type': 'permanent', 'id': str(n)}
            if sa != sb:
                section = 'verbs' if self.options.view == 'code' else 'objects'
                applicable = ('verbs',) if section == 'verbs' else ('objects', 'recycled') if 'recycled' in (sa, sb) else ('objects',)
                parent_selected = bool(self.sections.intersection(applicable)) and (self.options.view != 'code' or bool(a and a.verbs or b and b.verbs))
                if parent_selected:
                    self.emit(entity, 'code' if section == 'verbs' else 'definitions', section, [], [],
                              a if a is not None else MISSING, b if b is not None else MISSING,
                              'changed' if 'recycled' in (sa, sb) else 'added' if sa == 'absent' else 'removed',
                              details={'subtype': 'lifecycle', 'old_state': sa, 'new_state': sb,
                                       'added_lines': sum(len(v.code or []) for v in b.verbs) if b is not None and 'verbs' in self.sections else 0,
                                       'removed_lines': sum(len(v.code or []) for v in a.verbs) if a is not None and 'verbs' in self.sections else 0}, applicable=applicable)
                    if self.sections.intersection(applicable):
                        suppressed.add(n)
                else:
                    for content_section in ('properties', 'verbs'):
                        if content_section not in self.sections or not (a and getattr(a, content_section) or b and getattr(b, content_section)):
                            continue
                        details = {'subtype': 'property_container' if content_section == 'properties' else 'verb_container',
                                   'old_state': sa, 'new_state': sb}
                        if content_section == 'verbs':
                            details.update(added_lines=sum(len(v.code or []) for v in b.verbs) if b is not None else 0,
                                           removed_lines=sum(len(v.code or []) for v in a.verbs) if a is not None else 0)
                        payload_sections = {content_section} | ({'heap'} if 'heap' in self.sections else set())
                        self.emit(entity, 'definitions' if content_section == 'properties' else 'code', content_section,
                                  [_field(content_section)], [_field(content_section)], a if a is not None else MISSING,
                                  b if b is not None else MISSING, 'added' if a is None else 'removed', details=details,
                                  evidence_sections=payload_sections)
                continue
            if a is None or b is None:
                continue
            self.object_fields(a, b, entity)
            self.properties(a, b, entity)
            self.verbs(a, b, entity)
        global_entity = {'type': 'global', 'id': 'database'}
        for field, section in (('players', 'players'), ('recycled_objects', 'recycled')):
            av, bv = getattr(self.old, field), getattr(self.new, field)
            old_ids, new_ids = [int(n) for n in av], [int(n) for n in bv]
            if self.options.object_ids is not None:
                selected_ids = set(self.options.object_ids)
                old_ids = [n for n in old_ids if n in selected_ids]
                new_ids = [n for n in new_ids if n in selected_ids]
            before, after = set(old_ids), set(new_ids)
            for n in sorted(before ^ after):
                if section == 'recycled' and n in suppressed:
                    continue
                self.emit({'type': 'permanent', 'id': str(n)}, 'definitions', section, [_field(field)], [_field(field)],
                          ObjNum(n) if n in before else MISSING, ObjNum(n) if n in after else MISSING,
                          'removed' if n in before else 'added')
            survivors = before & after
            if section == 'players' and [n for n in old_ids if n in survivors] != [n for n in new_ids if n in survivors]:
                self.emit(global_entity, 'definitions', section, [_field(field)], [_field(field)],
                          [ObjNum(n) for n in old_ids], [ObjNum(n) for n in new_ids], 'order_changed')

    def heap(self):
        if self.options.view == 'checkpoint':
            for kind in ('anonymous', 'waif'):
                before = self.old.waifs if kind == 'waif' else {n: o for n, o in self.old.objects.items() if o.anon}
                after = self.new.waifs if kind == 'waif' else {n: o for n, o in self.new.objects.items() if o.anon}
                for n in sorted(before.keys() | after.keys()):
                    if self.options.object_ids is not None and (kind, n) not in self.reached['old'] | self.reached['new']:
                        continue
                    entity = {'type': kind, 'id': str(n)}
                    av, bv = before.get(n, MISSING), after.get(n, MISSING)
                    if kind == 'waif':
                        self.waif(av, bv, entity, n, n)
                    elif av is MISSING or bv is MISSING:
                        self.value(av, bv, entity, 'heap', 'heap', [], [])
                    else:
                        self.object_fields(av, bv, entity, True); self.properties(av, bv, entity, 'heap'); self.verbs(av, bv, entity, 'heap')
            return
        if 'properties' not in self.sections:
            for av, bv, anchor, op, np in self.roots:
                number = int(anchor['id'])
                both_present = number in self.old.objects and number in self.new.objects
                if not both_present and ('objects' in self.sections or 'recycled' in self.sections and
                                         (number in self.old.recycled_objects or number in self.new.recycled_objects)):
                    continue  # A selected lifecycle payload already owns this graph.
                if _contains_heap(av) or _contains_heap(bv):
                    self.value(av, bv, anchor, 'heap', 'heap', op, np, {'subtype': 'heap_root'})
        conflict_seen = set()
        for pair in sorted(self.pairs, key=lambda p: self.heap_ids[p]):
            ar, br = pair
            entity = {'type': ar[0], 'id': self.heap_ids[pair]}
            if ar in self.blocked_old or br in self.blocked_new:
                if ('old', ar) in conflict_seen or ('new', br) in conflict_seen:
                    continue
                component = {('old', ar), ('new', br)}
                previous = 0
                while len(component) != previous:
                    previous = len(component)
                    for p in self.pairs:
                        endpoints = {('old', p[0]), ('new', p[1])}
                        if component.intersection(endpoints):
                            component.update(endpoints)
                conflict_seen.update(component)
                paths = [{'entity': e['entity'], 'old_path': list(e['old_path']), 'new_path': list(e['new_path'])}
                         for p, evidence in self.proposals.items()
                         if component.intersection({('old', p[0]), ('new', p[1])}) for e in evidence]
                refs_a = [WaifReference(r[1]) if r[0] == 'waif' else Anon(r[1]) for side, r in sorted(component) if side == 'old']
                refs_b = [WaifReference(r[1]) if r[0] == 'waif' else Anon(r[1]) for side, r in sorted(component) if side == 'new']
                self.emit(entity, 'heap', 'heap', [], [], refs_a, refs_b, 'identity_conflict', True,
                          {'reason': 'non-bijective anchored correspondence', 'competing_paths': sorted(paths, key=canonical_json), 'anchors': sorted(paths, key=canonical_json)})
                continue
            av, bv = self.node(ar, 'old'), self.node(br, 'new')
            if av is MISSING or bv is MISSING:
                continue
            if ar[0] == 'waif':
                self.waif(av, bv, entity, ar[1], br[1])
            else:
                self.object_fields(av, bv, entity, True); self.properties(av, bv, entity, 'heap'); self.verbs(av, bv, entity, 'heap')

    def waif(self, a, b, entity, old_id, new_id):
        if a is MISSING or b is MISSING:
            self.value(a, b, entity, 'heap', 'heap', [], [])
            return
        if self.options.view != 'checkpoint':
            from .inspection import waif_slot_names
            unresolved_sides = []
            names = {}
            for side, database, waif in (('old', self.old, a), ('new', self.new, b)):
                cls = database.objects.get(int(waif.waif_class))
                if cls is None or cls.anon or any(int(p) != -1 and int(p) not in database.objects
                                                 for ancestor in database.ancestors(cls) for p in ancestor.parents):
                    unresolved_sides.append(side)
                    names[side] = []
                else:
                    names[side] = waif_slot_names(database, int(waif.waif_class))
            if unresolved_sides:
                self.emit(entity, 'heap', 'heap', [_field('waif_class')], [_field('waif_class')], a.waif_class, b.waif_class,
                          'identity_unresolved', False, {'reason': 'missing or invalid waif class ancestry', 'sides': unresolved_sides})
        else:
            names = {'old': [], 'new': []}
        for field in ('waif_class', 'owner', 'propdefs_length'):
            self.value(getattr(a, field), getattr(b, field), entity, 'heap', 'heap', [_field(field)], [_field(field)], {'subtype': 'waif_metadata'})
        before, after = dict(a.props), dict(b.props)
        if len(before) != len(a.props) or len(after) != len(b.props):
            raise ValueError('duplicate sparse waif property slot')
        for slot in sorted(before.keys() | after.keys()):
            op = [{'type': 'heap', 'kind': 'waif', 'id': entity['id'], 'raw_id': str(old_id), 'class': str(a.waif_class), 'slot': str(slot)}]
            np = [{'type': 'heap', 'kind': 'waif', 'id': entity['id'], 'raw_id': str(new_id), 'class': str(b.waif_class), 'slot': str(slot)}]
            self.value(before.get(slot, MISSING), after.get(slot, MISSING), entity, 'heap', 'heap', op, np,
                       {'subtype': 'waif_property', 'old_name': names['old'][slot] if 0 <= slot < len(names['old']) else None,
                        'new_name': names['new'][slot] if 0 <= slot < len(names['new']) else None})
        if self.options.view == 'checkpoint' and [s for s, _ in a.props] != [s for s, _ in b.props] and set(before) == set(after):
            self.emit(entity, 'heap', 'heap', [_field('props')], [_field('props')], [s for s, _ in a.props], [s for s, _ in b.props], 'order_changed')

    def validate(self):
        verify_field_coverage()
        from .inspection import verb_signature
        for side, database in (('old', self.old), ('new', self.new)):
            if hasattr(database, 'version') and database.version not in (1, 2, 3, 4, 17):
                self.error('unsupported format version', side=side, version=database.version)
            for n, o in database.objects.items():
                if n != o.id:
                    self.error('object dictionary key does not match record id', side=side, id=str(n))
                if n in database.recycled_objects:
                    self.error('present record also has recycled membership', side=side, id=str(n))
                for v in o.verbs:
                    if v.object != o.id:
                        self.error('derived Verb.object invariant failed', side=side, id=str(n))
                    try:
                        verb_signature(v)
                    except ValueError as exc:
                        self.error(str(exc), side=side, id=str(n))
            # Parsed counts are format-specific: version 17 counts permanent
            # and recycled slots; anonymous records are a later section.
            expected_objects = len([o for o in database.objects.values() if not o.anon]) + len(database.recycled_objects)
            if database.total_objects != expected_objects:
                self.error('declared object count contradicts parsed records', side=side,
                           declared=str(database.total_objects), parsed=str(expected_objects))
            if database.total_players != len(database.players):
                self.error('declared player count contradicts parsed records', side=side)
            if any(type(n) not in (int, ObjNum) for n in database.players):
                self.error('invalid player id type', side=side)
            else:
                player_ids = [int(n) for n in database.players]
                if len(player_ids) != len(set(player_ids)):
                    self.error('duplicate player id', side=side)
            programs = sum(v.code is not None for o in database.objects.values() for v in o.verbs)
            if database.total_verbs != programs:
                self.error('declared program count contradicts parsed records', side=side)
            try:
                self.validate_structure(database)
            except ValueError as exc:
                self.error(str(exc), side=side)

    def validate_structure(self, value):
        """Check each direct parsed container once, without heap serialization.

        References are scalars here. Object/waif tables supply every record,
        including orphan records; selected dangling targets remain reach checks.
        Active and completed identities distinguish cycles from shared values.
        """
        active, complete = set(), set()
        pending = [(False, value, False)]
        while pending:
            leaving, current, raw_table = pending.pop()
            identity = id(current)
            completed_key = (identity, raw_table)
            if leaving:
                active.remove(identity)
                complete.add(completed_key)
                continue
            kind = value_kind(current)
            if kind not in ('map', 'list', 'tuple', 'set', 'record'):
                continue
            if identity in active:
                raise ValueError('direct Python container cycle is not a parsed heap reference')
            if completed_key in complete:
                continue
            active.add(identity)
            pending.append((True, current, raw_table))
            if kind == 'map':
                keys = set()
                children = []
                for key, child in current.items():
                    # Database tables are exact dump-local record coordinates,
                    # not MOO MAP values subject to the configured comparator.
                    token = canonical_json(encode_value(key)) if raw_table else self.key(key, 'old', correspondence=False)
                    if token in keys:
                        raise ValueError('duplicate map keys under server equality')
                    keys.add(token)
                    children.extend(((key, False), (child, False)))
            elif kind == 'record':
                if type(current) not in FIELD_COVERAGE:
                    raise ValueError(f'undeclared comparison coverage for {type(current).__name__}')
                children = [(getattr(current, field.name, UNSET), type(current) is MooDatabase and field.name in ('objects', 'waifs'))
                            for field in attrs.fields(type(current))]
            else:
                children = [(child, False) for child in current]
            pending.extend((False, child, is_table) for child, is_table in reversed(children))

    def validate_maps(self, value):
        """Compatibility name for internal callers; also validates value types."""
        self.validate_structure(value)

    def run(self):
        try:
            self.validate()
        except (ValueError, RecursionError) as exc:
            self.error(str(exc) if not isinstance(exc, RecursionError) else 'parsed model nesting exceeds comparison recursion limit')
        try:
            if not self.errors:
                if self.options.view != 'checkpoint' and ('heap' in self.sections or 'properties' in self.sections):
                    self.correspondence()
                elif self.options.view == 'checkpoint':
                    self.prepare_roots()
                    for av, bv, _, _, _ in self.roots:
                        self.reach(av, 'old'); self.reach(bv, 'new')
                    for side, database in (('old', self.old), ('new', self.new)):
                        if 'runtime' in self.sections:
                            for field, section in FIELD_COVERAGE[MooDatabase].items():
                                if section == 'runtime':
                                    self.reach(getattr(database, field, UNSET), side)
                        if 'heap' in self.sections and self.options.object_ids is None:
                            for number in database.waifs:
                                self.reach(WaifReference(number), side)
                            for number, obj in database.objects.items():
                                if obj.anon:
                                    self.reach(Anon(number), side)
                self.permanent()
                if 'heap' in self.sections:
                    self.heap()
                for field, section in FIELD_COVERAGE[MooDatabase].items():
                    if section in ('runtime', 'serialization') and section in self.sections:
                        self.value(getattr(self.old, field, UNSET), getattr(self.new, field, UNSET),
                                   {'type': 'global', 'id': 'database'}, section, section, [_field(field)], [_field(field)])
        except _Stopped:
            pass
        except (ValueError, RecursionError) as exc:
            self.error(str(exc) if not isinstance(exc, RecursionError) else 'parsed model nesting exceeds comparison recursion limit')
            self.scan_complete = False
        self.events.sort(key=event_sort_key)
        excluded = [s for s in SECTIONS if s not in self.sections]
        header = {'record': 'header', 'schema_version': 1, 'tool_version': '0.1.10', 'view': self.options.view,
                  'options': self.options.normalized(), 'sections': list(self.options.sections), 'excluded_sections': excluded,
                  'identity_basis': 'object-number', 'lineage_assumption': 'paired permanent numeric slots; continuity between snapshots is not proven',
                  'policy_version': POLICY_VERSION, 'heap_identity_scope': 'selected permanent stored-property roots',
                  'numeric_map_key_policy': NUMERIC_MAP_KEY_POLICY,
                  'precision': 'parsed binary64 bits; original numeric spelling and NaN payload recovery are not claimed'}
        correspondence = [{'kind': pair[0][0], 'id': self.heap_ids[pair], 'old_id': str(pair[0][1]), 'new_id': str(pair[1][1]),
                           'anchors': self.anchors[self.heap_ids[pair]], 'resolved': pair[0] not in self.blocked_old and pair[1] not in self.blocked_new}
                          for pair in sorted(self.heap_ids, key=lambda p: self.heap_ids[p])]
        footer = {'record': 'footer', 'scan_complete': self.scan_complete and not self.errors, 'identity_complete': self.identity_complete and not self.blocked,
                  'details_complete': self.scan_complete and not self.errors, 'payloads_complete': True,
                  'errors': self.errors, 'excluded_sections': excluded, 'heap_correspondence': correspondence,
                  'orphan_heap_counts': {side: len(({('waif', number) for number in database.waifs} |
                                               {('anonymous', number) for number, obj in database.objects.items() if obj.anon}) - self.reached[side])
                                         for side, database in (('old', self.old), ('new', self.new))} if self.options.view != 'checkpoint' else {'old': 0, 'new': 0},
                  'corpus_validation': 'synthetic/parsed-model comparison; server restore validation is separate'}
        return finalize_counts(Comparison(header, self.events, footer))


def compare_snapshots(old_db: MooDatabase, new_db: MooDatabase, options: ReportOptions = ReportOptions()) -> Comparison:
    """Compare complete parsed snapshots using the declared exact report policy."""
    return _SnapshotComparison(old_db, new_db, options).run()
