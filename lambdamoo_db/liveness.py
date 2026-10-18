"""Which objects nothing live can reach: a mark and sweep over every object-number slot in a dump.

Roots are #0 (and so every $name), every player, every test suite (an object owning a `test_*` verb,
reached by typing `@test #N`), and every slot held by no object: tasks, the dump's player list, pending
values, connections, anonymous objects. From a reached object, every number it holds
is reached: property values, verb code literals, location, contents, parents, and the waifs its
properties hold. Not followed: children lists (a class does not keep its instances alive), owners and
owned lists (a player keeps nothing alive by owning it), `last_move` (history), `.lost_souls`, and any
property named in ``skip`` (bookkeeping such as a test's snapshot of the whole object list).
"""

import re
from collections.abc import Iterable

from .database import MooDatabase
from .objnums import remap
from .removal import SET_LISTS, _field, _holder, waif_holders

NOT_FOLLOWED = frozenset({"children", "owner", "verb.owner", "verb.object", "last_move"}) | SET_LISTS


def edges(db: MooDatabase, skip: Iterable[str] = ()) -> tuple[dict[int, set[int]], set[int]]:
    """Object -> the objects it holds, and the numbers held by no object (roots)."""
    skipped = NOT_FOLLOWED | {(s if s.startswith(".") else f".{s}").lower() for s in skip}
    holders = waif_holders(db)
    out: dict[int, set[int]] = {}
    roots: set[int] = set()

    def visit(kind: str, number: int, where: tuple) -> int:
        if kind != "object":
            return number
        field = _field(where).lower()  # MOO property names ignore case
        if field in skipped or field.endswith(" owner"):
            return number
        holder = _holder(where, holders)
        if holder is None:
            roots.add(number)
        else:
            for h in holder:
                out.setdefault(h, set()).add(number)
        return number

    remap(db, visit)
    return out, roots


def suites(db: MooDatabase) -> list[int]:
    """Objects owning a `test_*` verb: test suites, reached by someone typing `@test #N`, so roots."""
    return [oid for oid, o in db.objects.items()
            if any(name.startswith("test_") for v in o.verbs for name in v.name.split())]


MENTION = re.compile(r"#(\d+)")


def mentions(db: MooDatabase, numbers: set[int], ignore: frozenset[int] | set[int] = frozenset()) -> dict[int, set[str]]:
    """Where a number in ``numbers`` is spelled `#N` inside a string: property and waif values, and verb
    code outside object literals (a string a verb may hand to toobj()), leaving out what objects in
    ``ignore`` hold. The sweep cannot follow these."""
    found: dict[int, set[str]] = {}

    def scan(value, where: str) -> None:
        if isinstance(value, str):
            for m in MENTION.finditer(value):
                if int(m.group(1)) in numbers:
                    found.setdefault(int(m.group(1)), set()).add(where)
        elif isinstance(value, (list, tuple)):
            for item in value:
                scan(item, where)
        elif isinstance(value, dict):
            for k, v in value.items():
                scan(k, where)
                scan(v, where)

    for oid, o in db.objects.items():
        if oid in ignore:
            continue
        for p in o.properties:
            scan(p.value, f"#{oid}.{p.propertyName}")
        for v in o.verbs:
            for line in v.code or ():
                for m in re.finditer(r'"(?:[^"\\]|\\.)*"', line):  # string literals only
                    scan(m.group(0), f"#{oid}:{v.name}")
    for index, waif in db.waifs.items():
        for _, value in waif.props:
            scan(value, f"waif {index}")
    return found


def unreachable(db: MooDatabase, skip: Iterable[str] = (), gone: frozenset[int] | set[int] = frozenset()) -> set[int]:
    """Permanent objects no root reaches, outside ``gone`` (objects already being removed, which reach
    nothing and keep nothing alive)."""
    held, roots = edges(db, skip)
    reached: set[int] = set()
    todo = [0, *(int(p) for p in db.players), *roots, *suites(db)]
    while todo:
        oid = todo.pop()
        if oid in reached or oid not in db.objects or oid in gone:
            continue
        reached.add(oid)
        todo.extend(held.get(oid, ()))
    return {oid for oid, o in db.objects.items() if not o.anon} - reached - gone
