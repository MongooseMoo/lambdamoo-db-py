"""
Database comparison module for lambdamoo-db-py.

Provides structured diffs between two MooDatabase instances.
"""

from __future__ import annotations
import enum
import math
from typing import Any, Iterator
import attrs

from .database import Clear, WaifReference, MooObject, Verb, Property, Waif, MooDatabase


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


def compare_properties(
    path: DiffPath, expected: list[Property], actual: list[Property]
) -> list[Diff]:
    """Compare two property lists."""
    diffs: list[Diff] = []

    # Build dicts keyed by property name
    exp_by_name = {p.propertyName: p for p in expected}
    act_by_name = {p.propertyName: p for p in actual}

    all_names = set(exp_by_name.keys()) | set(act_by_name.keys())

    for name in sorted(all_names, key=str):
        prop_path = path.child("properties").child(name)

        if name not in act_by_name:
            diffs.append(Diff(prop_path, DiffKind.MISSING, exp_by_name[name], None))
        elif name not in exp_by_name:
            diffs.append(Diff(prop_path, DiffKind.EXTRA, None, act_by_name[name]))
        else:
            exp_prop = exp_by_name[name]
            act_prop = act_by_name[name]

            # Compare value
            diffs.extend(compare_values(prop_path.child("value"), exp_prop.value, act_prop.value))

            # Compare owner
            if exp_prop.owner != act_prop.owner:
                diffs.append(Diff(
                    prop_path.child("owner"),
                    DiffKind.VALUE_CHANGED,
                    exp_prop.owner,
                    act_prop.owner,
                ))

            # Compare perms
            if exp_prop.perms != act_prop.perms:
                diffs.append(Diff(
                    prop_path.child("perms"),
                    DiffKind.VALUE_CHANGED,
                    exp_prop.perms,
                    act_prop.perms,
                ))

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
