"""Read-only inspection of a textdump: resolve objects, verbs and properties.

The ``moodb`` CLI (``lambdamoo_db.cli:moodb``) is a thin layer over these
functions. Lookups follow ToastStunt where it matters: ``$name`` means
``#0.name``, verb names match like ``verbcasecmp()``, property names are
case-insensitive, and verbs and ``clear`` property values are inherited
through parents in ``db_ancestors()`` order.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any, Iterator

import attrs

from .database import (
    CLEAR,
    VM,
    Activation,
    Anon,
    MooCatch,
    MooDatabase,
    MooError,
    MooFinally,
    MooObject,
    ObjNum,
    Property,
    Verb,
    Waif,
    WaifReference,
)
from .enums import ObjectFlags, PropertyFlags
from .lazy import LazyDatabase
from .references import find_property_references

ERROR_NAMES = [
    "E_NONE", "E_TYPE", "E_DIV", "E_PERM", "E_PROPNF", "E_VERBNF", "E_VARNF",
    "E_INVIND", "E_RECMOVE", "E_MAXREC", "E_RANGE", "E_ARGS", "E_NACC",
    "E_INVARG", "E_QUOTA", "E_FLOAT", "E_FILE", "E_EXEC", "E_INTRPT",
]

# db_verbs.cc prep_list; the index is the stored prep value.
PREPOSITIONS = [
    "with/using", "at/to", "in front of", "in/inside/into",
    "on top of/on/onto/upon", "out of/from inside/from", "over", "through",
    "under/underneath/beneath", "behind", "beside", "for/about", "is", "as",
    "off/off of",
]
PREP_ANY = -2
PREP_NONE = -1
ARG_NAMES = ["none", "any", "this"]

VERB_READ, VERB_WRITE, VERB_EXEC, VERB_DEBUG = 1, 2, 4, 8


class LookupFailed(Exception):
    pass


# --------------------------------------------------------------------------
# Object references


_REF_RE = re.compile(r"^(\$[A-Za-z_][A-Za-z0-9_]*|#?-?[0-9]+)")


def split_ref(spec: str) -> tuple[str, str]:
    """Split ``$httpd:GET`` / ``#852.prop`` into (``$httpd``, ``:GET``)."""
    m = _REF_RE.match(spec)
    if not m:
        raise LookupFailed(f"not an object reference: {spec!r} (use #N, N or $name)")
    return m.group(1), spec[m.end():]


def resolve_object(db: MooDatabase, ref: str) -> MooObject:
    """Resolve an object reference, optionally traversing object-valued properties."""
    root, *path = ref.split(".")
    if not _REF_RE.fullmatch(root) or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in path
    ):
        raise LookupFailed(f"not an object reference: {ref!r} (use #N, N or $name, optionally .property)")
    if root.startswith("$"):
        value = property_value(db, db.objects[0], root[1:])
        if not isinstance(value, ObjNum):
            raise LookupFailed(f"#0.{root[1:]} is {format_value(value)}, not an object")
        num = int(value)
    else:
        num = int(root.lstrip("#"))
    obj = db.objects.get(num)
    if obj is None:
        raise LookupFailed(f"#{num} does not exist (recycled or out of range)")
    for part in path:
        value = property_value(db, obj, part)
        if not isinstance(value, ObjNum):
            raise LookupFailed(f"#{obj.id}.{part} is {format_value(value)}, not an object")
        num = int(value)
        obj = db.objects.get(num)
        if obj is None:
            raise LookupFailed(f"#{num} does not exist (recycled or out of range)")
    return obj


def descendants(db: MooDatabase, obj: MooObject, field: str = "children") -> Iterator[MooObject]:
    """Objects reachable through ``children`` or ``contents``, depth-first, each once."""
    seen = {obj.id}
    stack = [obj]
    while stack:
        o = stack.pop()
        found = [db.objects[int(c)] for c in getattr(o, field) if int(c) in db.objects and int(c) not in seen]
        seen.update(c.id for c in found)
        for c in found:
            yield c
        stack.extend(reversed(found))


def tree_summary(db: MooDatabase, obj: MooObject, field: str = "children") -> dict[str, int | str]:
    """Count reachable objects without listing names or property values.

    The root is excluded from object counts. Depth is the shortest distance
    from the root, so shared descendants have a stable depth. Missing and
    repeated references count stored edges across all visited objects; repeated
    edges include duplicates, shared descendants and cycles, not just cycles.
    A leaf is a descendant with no stored edges in the chosen relation.
    """
    if field not in ("children", "contents"):
        raise ValueError(f"not a tree relation: {field!r}")
    counts = {
        "root": obj.id,
        "relation": field,
        "direct_count": 0,
        "descendant_count": 0,
        "leaf_count": 0,
        "max_depth": 0,
        "missing_reference_count": 0,
        "repeated_reference_count": 0,
    }
    seen = {obj.id}
    pending = deque([(obj, 0)])
    while pending:
        current, depth = pending.popleft()
        links = getattr(current, field)
        if depth and not links:
            counts["leaf_count"] += 1
        for ref in links:
            number = int(ref)
            if number not in db.objects:
                counts["missing_reference_count"] += 1
            elif number in seen:
                counts["repeated_reference_count"] += 1
            else:
                seen.add(number)
                if depth == 0:
                    counts["direct_count"] += 1
                counts["descendant_count"] += 1
                counts["max_depth"] = max(counts["max_depth"], depth + 1)
                pending.append((db.objects[number], depth + 1))
    return counts


def dollar_names(db: MooDatabase) -> dict[int, str]:
    """Map object number to its ``$name`` from #0's own properties."""
    names: dict[int, str] = {}
    for p in own_properties(db.objects[0]):
        if isinstance(p.propertyName, str) and isinstance(p.value, ObjNum):
            names.setdefault(int(p.value), "$" + p.propertyName)
    return names


def object_flags(obj: MooObject) -> str:
    """Flag names as `@display` shows them (player, programmer, wizard, r, w, f), obsolete bits omitted."""
    shown = [("player", ObjectFlags.USER), ("programmer", ObjectFlags.PROGRAMMER), ("wizard", ObjectFlags.WIZARD),
             ("r", ObjectFlags.READ), ("w", ObjectFlags.WRITE), ("f", ObjectFlags.FERTILE),
             ("anonymous", ObjectFlags.ANONYMOUS), ("invalid", ObjectFlags.INVALID), ("recycled", ObjectFlags.RECYCLED)]
    return " ".join(name for name, bit in shown if obj.flags & bit) or "-"


def property_perms(prop: Property) -> str:
    return "".join(c for bit, c in zip((PropertyFlags.READ, PropertyFlags.WRITE, PropertyFlags.CLEAR), "rwc") if prop.perms & bit)


def label(db: MooDatabase, num: int, names: dict[int, str] | None = None) -> str:
    """``#20 $string_utils "string utilities"``; the name is omitted for missing objects."""
    num = int(num)
    name = db.name_of(num)
    text = f"#{num}"
    if names and num in names:
        text += f" {names[num]}"
    if name is not None:
        text += f" {moo_string(name)}"
    return text


# --------------------------------------------------------------------------
# Verbs

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def verbcasecmp(verb: str, word: str) -> bool:
    """Port of ToastStunt utils.cc verbcasecmp(): does WORD match any alias in the verb name VERB?

    Aliases are space-separated. A ``*`` in an alias marks where abbreviation
    may start (``foo*bar`` matches foo..foobar); a trailing ``*`` matches any
    continuation (``foo*`` matches foo, foobar, ...). Case is folded for ASCII only.
    """
    v, w = verb.translate(_ASCII_LOWER), word.translate(_ASCII_LOWER)
    i, n = 0, len(v)
    while i < n:
        j = 0
        star = None  # None, "inner" or "end", as in the C enum
        while True:
            while i < n and v[i] == "*":
                i += 1
                star = "end" if i == n or v[i] == " " else "inner"
            if i == n or v[i] == " " or j == len(w) or w[j] != v[i]:
                break
            i += 1
            j += 1
        if (star is not None or i == n or v[i] == " ") if j == len(w) else star == "end":
            return True
        while i < n and v[i] != " ":
            i += 1
        while i < n and v[i] == " ":
            i += 1
    return False


def waif_spelling(word: str) -> str:
    """The name a waif class verb is stored under: ``waif:word()`` runs the verb ``:word`` of the waif's class."""
    return word if word.startswith(":") else ":" + word


def answers_call(verb: str, word: str) -> bool:
    """Would a call to WORD run the verb named VERB, on an object or, as a waif class verb, on a waif?"""
    return verbcasecmp(verb, word) or verbcasecmp(verb, waif_spelling(word))


@attrs.frozen
class VerbHit:
    obj: MooObject
    index: int
    verb: Verb


_INDEX_RE = re.compile(r"[0-9]+")


def find_verb(db: MooDatabase, obj: MooObject, wanted: str, inherited: bool = True) -> VerbHit:
    """Find a verb by name (verbcasecmp) or by 0-based ``N`` index on obj itself.

    Unlike a MOO call, the x bit is not required, so non-executable command
    verbs are found too. Waif class verbs are stored as ``:name``; that
    spelling is tried when nothing matches the plain name.
    """
    if _INDEX_RE.fullmatch(wanted):
        idx = int(wanted)
        if idx >= len(obj.verbs):
            raise LookupFailed(f"#{obj.id} has only {len(obj.verbs)} verbs (indexes are 0-based)")
        return VerbHit(obj, idx, obj.verbs[idx])
    search = db.ancestors(obj) if inherited else [obj]
    for name in dict.fromkeys((wanted, waif_spelling(wanted))):
        for o in search:
            for idx, v in enumerate(o.verbs):
                if verbcasecmp(v.name, name):
                    return VerbHit(o, idx, v)
    raise LookupFailed(f"verb {wanted!r} not found on #{obj.id}{' or its ancestors' if inherited else ''}")


def verb_perms(verb: Verb) -> str:
    return "".join(c for bit, c in zip((VERB_READ, VERB_WRITE, VERB_EXEC, VERB_DEBUG), "rwxd") if verb.perms & bit)


def verb_signature(verb: Verb) -> tuple[str, int, int, int]:
    """The exact definition identity, shared by inspection and full diff."""
    dobj = (verb.perms >> 4) & 3
    iobj = (verb.perms >> 6) & 3
    if dobj == 3 or iobj == 3:
        raise ValueError('reserved verb argument encoding (3)')
    return verb.name, dobj, verb.preps, iobj


def verb_args(verb: Verb) -> str:
    _, dobj_bits, _, iobj_bits = verb_signature(verb)
    dobj = ARG_NAMES[dobj_bits]
    iobj = ARG_NAMES[iobj_bits]
    if verb.preps == PREP_ANY:
        prep = "any"
    elif verb.preps == PREP_NONE:
        prep = "none"
    elif 0 <= verb.preps < len(PREPOSITIONS):
        prep = PREPOSITIONS[verb.preps]
    else:
        prep = f"prep{verb.preps}"
    return f"{dobj} {prep} {iobj}"


def verb_summary(verb: Verb) -> str:
    """``"GET HEAD"  this none this  rxd  owner #2``"""
    return f"{moo_string(verb.name)}  {verb_args(verb)}  {verb_perms(verb)}  owner #{int(verb.owner)}"


def all_objects(db: MooDatabase) -> Iterator[MooObject]:
    return iter(sorted(db.objects.values(), key=lambda o: o.id))


def object_heads(db: MooDatabase) -> Iterator[MooObject]:
    """Every object in number order, for its header fields and verb definitions.

    Through an offset index the objects come without properties or verb code,
    which is most of a dump; ``all_objects`` parses all of it.
    """
    return db.object_heads() if isinstance(db, LazyDatabase) else all_objects(db)


@attrs.frozen
class GrepHit:
    obj: MooObject
    index: int
    verb: Verb
    lineno: int  # 1-based
    line: str


def grep_verbs(db: MooDatabase, pattern: re.Pattern[str], objs: list[MooObject] | None = None) -> Iterator[GrepHit]:
    """Yield a hit for every verb code line matching pattern."""
    if objs is None:
        objs = db.objects_with_programs() if isinstance(db, LazyDatabase) else list(all_objects(db))
    for o in objs:
        for idx, v in enumerate(o.verbs):
            for n, line in enumerate(v.code or [], 1):
                if pattern.search(line):
                    yield GrepHit(o, idx, v, n, line)


def _verb_keys(obj: MooObject) -> dict[tuple[str, str, int], tuple[int, Verb]]:
    """Each verb of obj under (names, arg spec, nth verb with those), so reordering does not unpair verbs."""
    seen: Counter[tuple[str, str]] = Counter()
    keyed = {}
    for idx, v in enumerate(obj.verbs):
        base = (v.name, verb_args(v))
        keyed[(*base, seen[base])] = (idx, v)
        seen[base] += 1
    return keyed


@attrs.frozen
class AddedVerbLine(GrepHit):
    is_new: bool


def added_verb_lines(old: MooDatabase, new: MooDatabase, pattern: re.Pattern[str] | None = None,
                     objs: list[MooObject] | None = None, invert: bool = False) -> Iterator[AddedVerbLine]:
    """Yield every verb code line in new that old's version of the same verb does not have.

    Verbs pair by object number, exact names, argument spec and occurrence
    among identical definitions. Lines align after stripping whitespace;
    insertions and replacements are reported with their original text.
    Moved lines can appear as additions. A verb or object that
    is not in old has every line reported. With pattern, only added lines
    matching it are yielded, or with invert only those that do not match.
    With objs (objects of new), only their verbs.
    """
    if objs is None:
        before_objects = old.objects_with_programs() if isinstance(old, LazyDatabase) else all_objects(old)
        old_by_id = {o.id: o for o in before_objects}
        after_objects = new.objects_with_programs() if isinstance(new, LazyDatabase) else all_objects(new)
    else:
        old_by_id = {}
        after_objects = sorted({o.id: o for o in objs}.values(), key=lambda o: o.id)
    for o in after_objects:
        before = old_by_id.get(o.id)
        if before is None:
            # An existing object may have definitions but no programs yet.
            before = old.objects.get(o.id)
        old_verbs = _verb_keys(before) if before is not None else {}
        for key, (idx, v) in _verb_keys(o).items():
            code = [line.strip() for line in v.code or []]
            was = [line.strip() for line in (old_verbs[key][1].code or [])] if key in old_verbs else []
            matcher = difflib.SequenceMatcher(None, was, code, autojunk=False)
            for tag, _, _, j1, j2 in matcher.get_opcodes():
                if tag not in ("insert", "replace"):
                    continue
                for n in range(j1, j2):
                    if pattern is None or bool(pattern.search(v.code[n])) != invert:
                        yield AddedVerbLine(o, idx, v, n + 1, v.code[n], key not in old_verbs)


def object_continuity(old: MooDatabase, new: MooDatabase, num: int, identity: str = "creation_time") -> str:
    """How #num in new relates to #num in old: "absent", "reused", "same" or "unproven".

    "absent": old has no object with that number. "reused": both have one and
    their identity property differs, so the number was recycled and given to
    another object in between. "same": the identity property is equal.
    "unproven": either side lacks the property or holds no value for it, so
    only the number says they are one object.
    """
    before, after = old.objects.get(num), new.objects[num]
    if before is None:
        return "absent"
    try:
        was, now = lookup_property(old, before, identity).value, lookup_property(new, after, identity).value
    except LookupFailed:
        return "unproven"
    if was is CLEAR or now is CLEAR:
        return "unproven"
    return "same" if was == now else "reused"


# --------------------------------------------------------------------------
# Properties


def own_properties(obj: MooObject) -> list[Property]:
    return obj.properties[: obj.propdefs_count]


def defined_property_names(db: MooDatabase) -> Iterator[tuple[int, list[str]]]:
    """(object number, the names of the properties it defines) for every object, in number order."""
    if isinstance(db, LazyDatabase):
        return db.defined_property_names()
    return ((o.id, [p.propertyName for p in own_properties(o)]) for o in all_objects(db))


@attrs.frozen
class AddedProperty:
    obj: MooObject  # the object of new that defines it
    prop: Property


def added_property_definitions(old: MooDatabase, new: MooDatabase, pattern: re.Pattern[str] | None = None,
                               objs: list[MooObject] | None = None, invert: bool = False) -> Iterator[AddedProperty]:
    """Yield every property new defines on an object that old's object of the same number does not define.

    Objects pair by number and definitions by name, ignoring letter case as
    the server does. An object that is not in old has every definition
    reported. Removed definitions and changed values, owners and permissions
    are not reported. With pattern, only names matching it are yielded, or
    with invert only those that do not match. With objs (objects of new),
    only their definitions.

    Without objs every object of both dumps is read: an offset index says
    where an object's record is, not whether it defines properties.
    """
    if objs is None:
        after_objects = all_objects(new)
        list(all_objects(old))  # one pass over old, not one read for each object of new
    else:
        after_objects = iter(sorted({o.id: o for o in objs}.values(), key=lambda o: o.id))
    for o in after_objects:
        defined = own_properties(o)
        if not defined:
            continue
        before = old.objects.get(o.id)
        had = {p.propertyName.lower() for p in own_properties(before)} if before is not None else set()
        for p in defined:
            if p.propertyName.lower() in had:
                continue
            if pattern is None or bool(pattern.search(p.propertyName)) != invert:
                yield AddedProperty(o, p)


def value_holders(db: MooDatabase, definer: MooObject, name: str) -> tuple[int, int]:
    """How many of definer and its descendants hold their own value of definer's property, and how many there are.

    An object holds a value when its slot is not clear; the definer always does.
    """
    holding = total = 0
    for o in (definer, *descendants(db, definer)):
        total += 1
        found = find_slot(db, o, name)
        if found is not None and found.definer is definer and found.index < len(o.properties):
            holding += o.properties[found.index].value is not CLEAR
    return holding, total


# db.h BUILTIN_PROPERTIES; flag properties read as 0 or 1.
_FLAG_PROPS = {"programmer": ObjectFlags.PROGRAMMER, "wizard": ObjectFlags.WIZARD, "r": ObjectFlags.READ,
               "w": ObjectFlags.WRITE, "f": ObjectFlags.FERTILE, "a": ObjectFlags.ANONYMOUS}
BUILTIN_PROPS = ("name", "owner", "location", "contents", "last_move", *_FLAG_PROPS)


def builtin_value(obj: MooObject, name: str) -> Any:
    name = name.lower()
    if name in _FLAG_PROPS:
        return int(bool(obj.flags & _FLAG_PROPS[name]))
    return {
        "name": lambda: obj.name,
        "owner": lambda: ObjNum(obj.owner),
        "location": lambda: ObjNum(obj.location),
        "contents": lambda: [ObjNum(c) for c in obj.contents],
        "last_move": lambda: obj.last_move,
    }[name]()


@attrs.frozen
class PropertySlot:
    definer: MooObject  # the ancestor (or obj itself) that defines the property
    index: int  # slot in obj.properties


def find_slot(db: MooDatabase, obj: MooObject, name: str) -> PropertySlot | None:
    """Locate a property like db_find_property(): own propdefs, then each ancestor's, case-insensitively."""
    wanted = name.lower()
    index = 0
    for a in db.ancestors(obj):
        for p in own_properties(a):
            if isinstance(p.propertyName, str) and p.propertyName.lower() == wanted:
                return PropertySlot(a, index)
            index += 1
    return None


@attrs.frozen
class PropertyHit:
    name: str  # as the definer spells it
    definer: MooObject
    slot: Property  # obj's own slot: its perms and owner apply
    value: Any  # effective value after following clear
    value_from: MooObject  # the object whose slot holds the value


def lookup_property(db: MooDatabase, obj: MooObject, name: str) -> PropertyHit:
    """Find a non-builtin property and its effective value, following ``clear`` like the server.

    A clear slot takes its value from the first parent that has the definer as
    an ancestor, repeatedly (db_find_property's clear loop).
    """
    found = find_slot(db, obj, name)
    if found is None:
        raise LookupFailed(f"property {name!r} not found on #{obj.id}")
    definer = found.definer
    if found.index >= len(obj.properties):
        raise LookupFailed(f"#{obj.id} has no slot {found.index} for .{name} (inconsistent dump)")
    slot = obj.properties[found.index]
    holder, value = obj, slot.value
    while value is CLEAR:
        parent = next((p for p in (db.objects.get(int(x)) for x in holder.parents)
                       if p is not None and any(a is definer for a in db.ancestors(p))), None)
        if parent is None:
            break
        where = find_slot(db, parent, name)
        if where is None or where.index >= len(parent.properties):
            break
        holder, value = parent, parent.properties[where.index].value
    return PropertyHit(own_name(definer, name), definer, slot, value, holder)


def own_name(definer: MooObject, name: str) -> str:
    return next(p.propertyName for p in own_properties(definer)
                if isinstance(p.propertyName, str) and p.propertyName.lower() == name.lower())


def property_value(db: MooDatabase, obj: MooObject, name: str) -> Any:
    """The effective value of a builtin or defined property, following ``clear`` up the parents."""
    if name.lower() in BUILTIN_PROPS:
        return builtin_value(obj, name)
    return lookup_property(db, obj, name).value


def all_properties(db: MooDatabase, obj: MooObject) -> Iterator[PropertyHit]:
    """Every defined property of obj (own first, then each ancestor's), with effective values."""
    for a in db.ancestors(obj):
        for p in own_properties(a):
            if isinstance(p.propertyName, str):
                yield lookup_property(db, obj, p.propertyName)


def stored_properties(db: MooDatabase, obj: MooObject) -> Iterator[tuple[str, Any]]:
    """(name, value) for every property whose slot on obj holds a value of its own (not clear)."""
    index = 0
    for a in db.ancestors(obj):
        for p in own_properties(a):
            if index < len(obj.properties) and isinstance(p.propertyName, str) and obj.properties[index].value is not CLEAR:
                yield p.propertyName, obj.properties[index].value
            index += 1


# --------------------------------------------------------------------------
# References


@attrs.frozen
class Reference:
    obj: MooObject
    where: str  # ".prop[1].key" or ":[3] verbname:12"
    text: str  # the matching code line, or ""


def find_references(db: MooDatabase, target: MooObject, names: dict[int, str]) -> Iterator[Reference]:
    """Property values holding ``#N``, then verb code lines mentioning ``#N`` or its ``$name``."""
    for path in find_property_references(db, target.id):
        num, _, index, _, *rest = path.segments
        o = db.objects[int(str(num).lstrip("#"))]
        prop = o.properties[int(index)].propertyName
        yield Reference(o, f".{prop}" + "".join(f"[{s}]" if isinstance(s, int) else f".{s}" for s in rest), "")
    words = [rf"(?<![\w#$-])#{target.id}(?![0-9])"]
    if target.id in names:
        words.append(rf"(?<![\w$])\{names[target.id]}(?![\w])")
    rx = re.compile("|".join(words))
    for hit in grep_verbs(db, rx):
        yield Reference(hit.obj, f":[{hit.index}] {hit.verb.name.split(' ')[0]}:{hit.lineno}", hit.line.strip())


# --------------------------------------------------------------------------
# Stored values


def waif_slot_names(db: MooDatabase, class_id: int) -> list[str]:
    """Names of a waif class's slots, by slot index, without the leading colon.

    A waif's slots are its class's ``:``-prefixed properties, own first and
    then each ancestor's (waif.cc gen_waif_propdefs).
    """
    cls = db.objects.get(class_id)
    if cls is None:
        return []
    return [p.propertyName[1:] for a in db.ancestors(cls) for p in own_properties(a)
            if isinstance(p.propertyName, str) and p.propertyName.startswith(":")]


@attrs.frozen
class StoredString:
    obj: MooObject  # the object whose property holds the value, directly or through waifs
    where: str  # ".prop[2]<waif #3010>.snd"; list indexes are 1-based like MOO
    value: str
    waif_classes: tuple[int, ...]  # classes of the waifs passed through, outermost first


def find_strings(db: MooDatabase, pattern: re.Pattern[str]) -> Iterator[StoredString]:
    """Every stored string matching ``pattern``, with the path to it.

    Searches each object's own (non-clear) property slots, anonymous objects
    included, through lists, map keys and values, and into waif bodies. A waif
    held in several places is reported once per holder. A waif that contains
    itself is entered once per path.
    """
    slot_names: dict[int, list[str]] = {}

    def walk(value: Any, where: str, classes: tuple[int, ...], open_waifs: frozenset[int]) -> Iterator[tuple[str, str, tuple[int, ...]]]:
        if isinstance(value, str):
            if pattern.search(value):
                yield where, value, classes
        elif isinstance(value, list):
            for i, item in enumerate(value, 1):
                yield from walk(item, f"{where}[{i}]", classes, open_waifs)
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from walk(key, f"{where}.keys[{_format(key)}]", classes, open_waifs)
                yield from walk(item, f"{where}[{_format(key)}]", classes, open_waifs)
        elif isinstance(value, WaifReference):
            waif = db.waifs.get(value.index)
            if waif is None or value.index in open_waifs:
                return
            cls = int(waif.waif_class)
            if cls not in slot_names:
                slot_names[cls] = waif_slot_names(db, cls)
            names = slot_names[cls]
            for slot, item in waif.props:
                name = names[slot] if slot < len(names) else f"slot{slot}"
                yield from walk(item, f"{where}<waif #{cls}>.{name}", (*classes, cls), open_waifs | {value.index})

    for obj in db.objects.values():
        names = [p.propertyName for a in db.ancestors(obj) for p in own_properties(a)]
        for index, prop in enumerate(obj.properties):
            if prop.value is CLEAR:
                continue
            name = names[index] if index < len(names) else f"slot{index}"
            for where, value, classes in walk(prop.value, f".{name}", (), frozenset()):
                yield StoredString(obj, where, value, classes)


# --------------------------------------------------------------------------
# Tasks


@attrs.frozen
class TaskInfo:
    kind: str  # queued, suspended, interrupted
    id: int
    when: int | None  # Unix seconds: when a queued/suspended task runs next
    frames: list[Activation]  # outermost first


def tasks(db: MooDatabase) -> list[TaskInfo]:
    def frames(vm: VM | None) -> list[Activation]:
        return [a for a in vm.stack if a is not None] if vm else []

    out = [TaskInfo("queued", t.id, t.st, [t.activation] if t.activation else []) for t in db.queuedTasks]
    out += [TaskInfo("suspended", t.id, t.startTime, frames(t.vm)) for t in db.suspendedTasks]
    out += [TaskInfo(f"interrupted ({t.status})", t.id, None, frames(t.vm)) for t in db.interruptedTasks]
    return out


def frame_text(a: Activation) -> str:
    """``#852:GET (this #852, player #2)``: vloc is where the running verb is defined."""
    return f"#{a.vloc}:{a.verb} (this #{a.this}, player #{a.player})"


def format_time(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# --------------------------------------------------------------------------
# Values


def moo_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_value(value: Any, limit: int | None = None) -> str:
    """Render a value as a MOO literal; ``limit`` truncates the result."""
    text = _format(value)
    if limit is not None and len(text) > limit:
        text = text[: max(limit - 3, 0)] + "..."
    return text


def _format(value: Any) -> str:
    if value is CLEAR:
        return "clear"
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, ObjNum):
        return f"#{int(value)}"
    if isinstance(value, Anon):
        return f"*anonymous #{int(value)}*"
    if isinstance(value, MooError):
        n = int(value)
        return ERROR_NAMES[n] if 0 <= n < len(ERROR_NAMES) else f"E_{n}"
    if isinstance(value, (MooCatch, MooFinally)):
        return repr(value)
    if isinstance(value, str):
        return moo_string(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "{" + ", ".join(_format(v) for v in value) + "}"
    if isinstance(value, dict):
        return "[" + ", ".join(f"{_format(k)} -> {_format(v)}" for k, v in value.items()) + "]"
    if isinstance(value, Waif):
        return f"<waif of #{value.waif_class}>"
    if isinstance(value, WaifReference):
        return f"<waif ref {value.index}>"
    return repr(value)
