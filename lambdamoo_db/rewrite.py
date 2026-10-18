"""Remove objects from a dump and number the rest densely, rewriting every reference.

``rewrite(db, chosen, owner_to=..., strand_to=...)`` changes ``db`` in place and returns a ``Report``.

- The removal set is ``removal.plan``'s: the chosen objects and all their descendants.
- Survivors keep their order: permanent objects become #0..#n-1, with no recycled slots; anonymous
  objects follow as n+k, the numbers ToastStunt gives them at write (db_objects.cc db_write_anonymous).
- A reference to a removed or already missing object becomes ``base - old``: distinct per old number,
  so verb literal tables keep their length and saved ``pc`` values stay valid; negative, so no later
  ``create()`` can bring it to life; and reversible, so the report can name what it was. ``base`` is
  ``DEAD_BASE``, or one below the lowest number already in the dump when an earlier rewrite left dead
  numbers there; those stay as they are, and the two ranges cannot meet.
- Entries naming dead objects are dropped from ``SET_LISTS`` properties, children and contents lists,
  and the player list.
- A surviving object, verb or property owned by a removed object is given to ``owner_to``; a surviving
  object located in a removed one is moved to ``strand_to``. Without them, either case refuses.
- A task whose frames run as, on, or from a removed object is dropped.
"""

from collections import Counter
from collections.abc import Iterable

import attrs

from .database import CLEAR, Anon, MooDatabase, ObjNum
from .objnums import _Walk, remap
from .removal import SET_LISTS, Plan, _field, plan

DEAD_BASE = -1_000_000
_TASK_FRAME_FIELDS = ("this", "programmer", "vloc", "player")
_TASK_SECTIONS = ("queuedTasks", "suspendedTasks", "interruptedTasks")


class RewriteRefused(ValueError):
    pass


def dead(old: int, base: int = DEAD_BASE) -> int:
    return base - old


@attrs.define
class Report:
    plan: Plan
    base: int  # dead number = base - old
    renumbered: dict[int, int]  # old -> new, permanent and anonymous
    dead_slots: Counter  # field -> slots now holding a dead number
    dropped: Counter  # list -> entries taken out
    reassigned: Counter  # what -> slots given to owner_to / strand_to
    tasks_dropped: list[tuple[str, int]]  # (section, task id)


def rewrite(db: MooDatabase, chosen: Iterable[int], owner_to: int | None = None,
            strand_to: int | None = None, with_contents: bool = False) -> Report:
    p = plan(db, chosen, with_contents)
    removed = p.removed
    base = _dead_base(db)
    reassigned = _reassign(db, p, owner_to, strand_to)
    tasks_dropped = _drop_tasks(db, removed)
    for oid in removed:
        del db.objects[oid]

    permanent = sorted(oid for oid, o in db.objects.items() if not o.anon)
    anons = sorted(oid for oid, o in db.objects.items() if o.anon)
    new_perm = {old: new for new, old in enumerate(permanent)}
    new_anon = {old: len(permanent) + k for k, old in enumerate(anons)}
    dropped = _drop_dead_entries(db, set(new_perm))

    dead_slots: Counter = Counter()

    def visit(kind: str, number: int, where: tuple) -> int:
        if kind == "anon":
            if number not in new_anon:
                raise RewriteRefused(f"anonymous reference {number} at {where} names no anonymous object")
            return new_anon[number]
        if number < 0:
            return number
        if number in new_perm:
            return new_perm[number]
        dead_slots[_field(where)] += 1
        return dead(number, base)

    remap(db, visit)
    objects = {}
    for old in permanent + anons:
        o = db.objects[old]
        o.id = new_perm.get(old, new_anon.get(old))
        objects[o.id] = o
    db.objects = objects
    db.total_objects = len(permanent)
    db.recycled_objects = set()
    return Report(p, base, {**new_perm, **new_anon}, dead_slots, dropped, reassigned, tasks_dropped)


def empty_property(db: MooDatabase, oid: int, name: str) -> int:
    """Empty ``name`` on ``oid`` (same type, no content) and clear every descendant's own value of it.

    For leftover data in a property code still writes, such as a test's stored baseline: the
    definition stays, so verbs that set it keep working. Returns how many slots changed.
    """
    target = db.objects[oid]
    changed = 0
    for o in [target, *_descendants(db, target)]:
        index = _slot(db, o, name)
        if index is None:
            raise RewriteRefused(f"#{o.id} has no property {name!r}")
        slot = o.properties[index]
        if o is target:
            empty = _EMPTY.get(type(slot.value))
            if empty is None:
                raise RewriteRefused(f"#{oid}.{name} holds a {type(slot.value).__name__}; no empty value for it")
            if slot.value != empty():
                slot.value = empty()
                changed += 1
        elif slot.value is not CLEAR:
            slot.value = CLEAR
            changed += 1
    return changed


_EMPTY = {list: list, dict: dict, str: str, int: int, float: float}


def _descendants(db: MooDatabase, o) -> list:
    out, todo = [], list(o.children)
    while todo:
        child = db.objects[int(todo.pop())]
        out.append(child)
        todo.extend(child.children)
    return out


def _slot(db: MooDatabase, o, name: str) -> int | None:
    """The index of ``name``'s slot in ``o.properties``: slots follow the ancestors' own definitions in order."""
    index = 0
    for a in db.ancestors(o):
        for p in a.properties[: a.propdefs_count]:
            if p.propertyName.lower() == name.lower():
                return index
            index += 1
    return None


def _dead_base(db: MooDatabase) -> int:
    """DEAD_BASE, or one below the lowest object number in the dump if that is lower."""
    lowest = [0]

    def visit(kind: str, number: int, where: tuple) -> int:
        if kind == "object" and number < lowest[0]:
            lowest[0] = number
        return number

    remap(db, visit)
    return min(DEAD_BASE, lowest[0] - 1)


def _reassign(db: MooDatabase, p: Plan, owner_to: int | None, strand_to: int | None) -> Counter:
    removed = p.removed
    owned = [(oid, what) for oid, o in db.objects.items() if oid not in removed
             for what in _owned_by_removed(o, removed)]
    if owned and owner_to is None:
        raise RewriteRefused(f"{len(owned)} surviving objects, verbs or properties are owned by removed objects, "
                             f"e.g. #{owned[0][0]} {owned[0][1]}; pass owner_to")
    if p.stranded and strand_to is None:
        oid, where = next(iter(p.stranded.items()))
        raise RewriteRefused(f"{len(p.stranded)} surviving objects are located in removed ones, e.g. #{oid} in #{where}; "
                             "pass strand_to")
    for target in (owner_to, strand_to):
        if target is not None and target >= 0 and (target in removed or target not in db.objects):
            raise RewriteRefused(f"#{target} is removed or does not exist")
    counts: Counter = Counter()
    for oid, o in db.objects.items():
        if oid in removed:
            continue
        if int(o.owner) in removed:
            o.owner = ObjNum(owner_to)
            counts["object owner"] += 1
        for v in o.verbs:
            if int(v.owner) in removed:
                v.owner = ObjNum(owner_to)
                counts["verb owner"] += 1
        for prop in o.properties:
            if int(prop.owner) in removed:
                prop.owner = ObjNum(owner_to)
                counts["property owner"] += 1
    for oid in sorted(p.stranded):
        db.objects[oid].location = ObjNum(strand_to)
        if strand_to >= 0:
            db.objects[strand_to].contents.append(ObjNum(oid))
        counts["moved out"] += 1
    return counts


def _owned_by_removed(o, removed: set[int]) -> list[str]:
    found = ["owner"] if int(o.owner) in removed else []
    found += [f"verb {v.name!r}" for v in o.verbs if int(v.owner) in removed]
    found += [f"property .{prop.propertyName}" for prop in o.properties if int(prop.owner) in removed]
    return found


def _drop_tasks(db: MooDatabase, removed: set[int]) -> list[tuple[str, int]]:
    dropped = []
    for section in _TASK_SECTIONS:
        tasks = getattr(db, section)
        keep = []
        for task in tasks:
            hits = []

            def visit(kind: str, number: int, where: tuple) -> int:
                if kind == "object" and number in removed and any(f in where for f in _TASK_FRAME_FIELDS):
                    hits.append(where)
                return number

            _Walk(visit).record(task, (section,), "object")
            if hits:
                dropped.append((section, task.id))
            else:
                keep.append(task)
        setattr(db, section, keep)
    return dropped


def _drop_dead_entries(db: MooDatabase, alive: set[int]) -> Counter:
    """Take dead objects out of set-like properties, children and contents lists, and the player list."""
    counts: Counter = Counter()

    def live(items: list, label: str) -> list:
        kept = [x for x in items if not (isinstance(x, (ObjNum, int)) and not isinstance(x, (bool, Anon))
                                         and 0 <= int(x) and int(x) not in alive)]
        counts[label] += len(items) - len(kept)
        return kept

    for o in db.objects.values():
        o.children = live(o.children, "children")
        o.contents = live(o.contents, "contents")
        for prop in o.properties:
            if f".{prop.propertyName}" in SET_LISTS and isinstance(prop.value, list):
                prop.value = live(prop.value, f".{prop.propertyName}")
    db.players = live(db.players, "players")
    return +counts
