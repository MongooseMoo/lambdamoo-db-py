"""What removing a set of objects from a dump would leave pointing at nothing.

``plan(db, chosen)`` closes the set (an object goes with all its descendants) and walks every
object-number slot with ``objnums.remap``. A slot naming a removed object from a holder that stays is
an inbound reference: the removal is clean only when there are none, apart from entries in the
set-like lists named in ``SET_LISTS``, which the rewrite drops.
"""

from collections import Counter
from collections.abc import Iterable
from typing import Any

import attrs

from .database import MooDatabase, WaifReference
from .objnums import remap

# Properties whose value is a set of objects kept for bookkeeping: an entry naming a removed object is
# dropped, not kept as a dangling reference.
SET_LISTS = frozenset({".owned_objects", ".lost_souls"})
# Objects' lists of their children and contents, and the dump's player list: the rewrite takes removed
# objects out of them.
STRUCTURAL = frozenset({"children", "contents", "players"})


@attrs.define
class Plan:
    removed: set[int]  # the closed set
    chosen: set[int]  # as given
    descendants: set[int]  # added because an ancestor is removed
    inbound: Counter  # (field, holder, target) -> slots, holder outside the set
    dropped: Counter  # set-list property or structural field -> entries dropped
    stranded: dict[int, int]  # object outside the set -> the removed object it is located in
    players: set[int]  # removed objects that are players


def plan(db: MooDatabase, chosen: Iterable[int]) -> Plan:
    """The closed removal set for ``chosen`` and every reference into it from outside."""
    chosen = {int(c) for c in chosen}
    removed = set(chosen)
    todo = list(chosen)
    while todo:
        o = db.objects.get(todo.pop())
        for child in o.children if o is not None else ():
            if int(child) not in removed:
                removed.add(int(child))
                todo.append(int(child))
    holders = waif_holders(db)
    inbound: Counter = Counter()
    dropped: Counter = Counter()

    def visit(kind: str, number: int, where: tuple) -> int:
        if kind != "object" or number not in removed:
            return number
        holder = _holder(where, holders)
        if holder is not None and holder <= removed:
            return number
        field = _field(where)
        if field in SET_LISTS or field in STRUCTURAL:
            dropped[field] += 1
        else:
            inbound[(field, _label(where, holder), number)] += 1
        return number

    remap(db, visit)
    stranded = {oid: int(o.location) for oid, o in db.objects.items()
                if oid not in removed and int(o.location) in removed}
    players = removed & {int(p) for p in db.players}
    return Plan(removed, chosen, removed - chosen, inbound, dropped, stranded, players)


def waif_holders(db: MooDatabase) -> dict[int, set[int]]:
    """Waif index -> the objects whose property values (directly or through other waifs) hold it."""
    direct: dict[int, set[int]] = {}

    def scan(value: Any, holder: int) -> None:
        if isinstance(value, WaifReference):
            direct.setdefault(value.index, set()).add(holder)
        elif isinstance(value, (list, tuple)):
            for item in value:
                scan(item, holder)
        elif isinstance(value, dict):
            for k, v in value.items():
                scan(k, holder)
                scan(v, holder)

    for oid, o in db.objects.items():
        for p in o.properties:
            scan(p.value, oid)
    # A waif held by a waif belongs to whoever holds the outer one.
    inner: dict[int, set[int]] = {}
    for index, waif in db.waifs.items():
        for _, value in waif.props:
            refs: dict[int, set[int]] = {}
            _collect(value, refs)
            for ref in refs:
                inner.setdefault(ref, set()).add(index)
    resolved: dict[int, set[int]] = {}

    def owners(index: int, seen: frozenset = frozenset()) -> set[int]:
        if index in resolved:
            return resolved[index]
        found = set(direct.get(index, ()))
        for outer in inner.get(index, ()):
            if outer not in seen:
                found |= owners(outer, seen | {index})
        resolved[index] = found
        return found

    return {index: owners(index) for index in set(direct) | set(inner)}


def _collect(value: Any, out: dict[int, set[int]]) -> None:
    if isinstance(value, WaifReference):
        out.setdefault(value.index, set())
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect(item, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            _collect(k, out)
            _collect(v, out)


def _holder(where: tuple, waifs: dict[int, set[int]]) -> set[int] | None:
    """The objects a slot belongs to; None for task and header slots, which no removal takes along."""
    head = where[0] if where else None
    if isinstance(head, str) and head.startswith("#"):
        return {int(head[1:])}
    if head == "waifs":
        return waifs.get(where[1]) or None
    return None


def _field(where: tuple) -> str:
    """`.name` for a property value, else the field path without indexes: `owner`, `verb.code`, `queuedTasks.activation.this`."""
    head, rest = where[0], list(where[1:])
    if isinstance(head, str) and head.startswith(("#", "anon ")):
        if rest and isinstance(rest[0], str) and rest[0][:1] == ".":
            return rest[0] if len(rest) > 1 and rest[1] == "value" else f"{rest[0]} {rest[1]}"
        if rest and isinstance(rest[0], str) and rest[0][:1] == ":":
            return f"verb.{rest[1]}"
        return str(rest[0]) if rest else "object"
    return ".".join(s for s in where if isinstance(s, str) and s not in ("key", "value"))


def _label(where: tuple, holder: set[int] | None) -> str:
    head = where[0]
    if isinstance(head, str) and head.startswith(("#", "anon ")):
        return head
    if head == "waifs":
        return f"waif {where[1]}" + (f" in {', '.join(f'#{h}' for h in sorted(holder)[:3])}" if holder else " (unheld)")
    return str(head)
