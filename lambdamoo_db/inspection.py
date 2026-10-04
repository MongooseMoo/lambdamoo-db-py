"""Read-only inspection of a textdump: resolve objects, verbs and properties.

The ``moodb`` CLI (``lambdamoo_db.cli:moodb``) is a thin layer over these
functions. Lookups follow ToastStunt where it matters: ``$name`` means
``#0.name``, verb names match like ``verbcasecmp()``, property names are
case-insensitive, and verbs and ``clear`` property values are inherited
through parents in ``db_ancestors()`` order.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
import sys
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
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
from .reader import load
from .references import find_property_references

logger = logging.getLogger(__name__)

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
# Loading with a pickle cache (parsing a 100 MB dump takes ~30 s)

# Modules whose code decides what a parsed dump looks like once pickled.
_CACHE_KEY_MODULES = ("reader.py", "database.py", "enums.py", "templates.py")


def _package_version() -> str:
    try:
        return version("lambdamoo-db")
    except PackageNotFoundError:
        return "dev"


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".cache") / "lambdamoo-db"


def _cache_prefix(db_path: Path) -> str:
    return hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()[:16]


def cache_path_for(db_path: Path, cache_dir: Path) -> Path:
    """``<path hash>-<content key hash>.pickle``; the key covers the dump and the parser."""
    st = db_path.stat()
    here = Path(__file__).parent
    parts = [str(st.st_size), str(st.st_mtime_ns), _package_version(), f"{sys.version_info[0]}.{sys.version_info[1]}"]
    parts += [hashlib.sha256((here / m).read_bytes()).hexdigest() for m in _CACHE_KEY_MODULES]
    key = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return cache_dir / f"{_cache_prefix(db_path)}-{key}.pickle"


def load_cached(db_path: str | Path, cache_dir: Path | None = None) -> MooDatabase:
    """Load a textdump, reusing a pickle keyed on path, size, mtime and parser source.

    Writing a new pickle for a dump removes the stale pickles of earlier
    versions of the same dump path, so the cache holds one entry per dump.
    An unreadable pickle is reparsed and replaced.
    """
    db_path = Path(db_path)
    if cache_dir is None:
        return load(str(db_path))
    cached = cache_path_for(db_path, cache_dir)
    if cached.exists():
        try:
            return pickle.loads(cached.read_bytes())
        except Exception as e:  # a truncated or incompatible pickle is only a cache miss
            logger.warning("ignoring unreadable cache %s: %s", cached, e)
    db = load(str(db_path))
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=cached.stem, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(db, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, cached)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    for stale in cache_dir.glob(f"{_cache_prefix(db_path)}-*.pickle"):
        if stale != cached:
            stale.unlink(missing_ok=True)
    return db


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
    obj = db.objects.get(num)
    text = f"#{num}"
    if names and num in names:
        text += f" {names[num]}"
    if obj is not None:
        text += f" {moo_string(obj.name)}"
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
    for name in (wanted,) if wanted.startswith(":") else (wanted, ":" + wanted):
        for o in search:
            for idx, v in enumerate(o.verbs):
                if verbcasecmp(v.name, name):
                    return VerbHit(o, idx, v)
    raise LookupFailed(f"verb {wanted!r} not found on #{obj.id}{' or its ancestors' if inherited else ''}")


def verb_perms(verb: Verb) -> str:
    return "".join(c for bit, c in zip((VERB_READ, VERB_WRITE, VERB_EXEC, VERB_DEBUG), "rwxd") if verb.perms & bit)


def verb_args(verb: Verb) -> str:
    dobj = ARG_NAMES[(verb.perms >> 4) & 3]
    iobj = ARG_NAMES[(verb.perms >> 6) & 3]
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
    return (db.objects[k] for k in sorted(db.objects))


@attrs.frozen
class GrepHit:
    obj: MooObject
    index: int
    verb: Verb
    lineno: int  # 1-based
    line: str


def grep_verbs(db: MooDatabase, pattern: re.Pattern[str], objs: list[MooObject] | None = None) -> Iterator[GrepHit]:
    """Yield a hit for every verb code line matching pattern."""
    for o in objs if objs is not None else all_objects(db):
        for idx, v in enumerate(o.verbs):
            for n, line in enumerate(v.code or [], 1):
                if pattern.search(line):
                    yield GrepHit(o, idx, v, n, line)


# --------------------------------------------------------------------------
# Properties


def own_properties(obj: MooObject) -> list[Property]:
    return obj.properties[: obj.propdefs_count]


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
