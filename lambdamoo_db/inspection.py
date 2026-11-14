"""Read-only inspection of a textdump: resolve objects, verbs and properties.

The ``moodb`` CLI (``lambdamoo_db.cli:moodb``) is a thin layer over these
functions. Lookups follow ToastStunt where it matters: ``$name`` means
``#0.name``, verb names match like ``verbcasecmp()``, property names are
case-insensitive, and verbs and ``clear`` property values are inherited
through parents in ``db_ancestors()`` order.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any, Callable, Iterator, NamedTuple, Sequence

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


def code_fingerprint(verb: Verb) -> dict[str, Any]:
    """Hash exact stored source bytes without returning source or stamp text.

    Dump strings preserve bytes through latin-1. Programs use LF after each
    stored line, matching FileIO writeline. An absent program differs from an
    explicitly empty one. Only a recognized UTC editor stamp exposes a date;
    arbitrary text after its date remains private.
    """
    lines = verb.code
    footer = lines[-1] if lines and lines[-1].startswith('"Last modified ') else None
    modified_utc = None
    if footer:
        match = re.match(r'^"Last modified (\w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}) UTC by ', footer)
        if match:
            try:
                stamp_date = datetime.strptime(match[1], "%a %b %d %H:%M:%S %Y")
                modified_utc = stamp_date.replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass
    source_bytes = "".join(line + "\n" for line in lines).encode("latin-1") if lines is not None else None
    return {
        "program_present": lines is not None,
        "line_count": len(lines) if lines is not None else None,
        "sha256": hashlib.sha256(source_bytes).hexdigest() if source_bytes is not None else None,
        "footer_sha256": hashlib.sha256(footer.encode("latin-1")).hexdigest() if footer is not None else None,
        "modified_utc": modified_utc,
    }

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


def mask_moo_strings(lines: list[str]) -> list[str]:
    """Mask string literals (including MOO's comment statements), preserving positions.

    This is lexical filtering, not a parser or proof that a branch executes.
    Escapes consume the next character inside strings. State crosses lines so
    unusual stored source cannot accidentally expose a string's contents.
    """
    quoted = escaped = False
    result = []
    for line in lines:
        masked = []
        for char in line:
            if quoted:
                masked.append(' ')
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                masked.append(' ')
                quoted = True
            else:
                masked.append(char)
        result.append(''.join(masked))
        escaped = False  # physical line boundary is not an escaped quote
    return result


def grep_verbs(db: MooDatabase, pattern: re.Pattern[str], objs: list[MooObject] | None = None, *,
               require: Sequence[re.Pattern[str]] = (), exclude: Sequence[re.Pattern[str]] = (),
               code_only: bool = False, owners: set[int] | None = None,
               wizard_owned: bool = False, executable: bool = False) -> Iterator[GrepHit]:
    """Yield matching source lines, with optional whole-verb and metadata filters.

    Every require pattern must match some line; any exclude match omits the
    whole verb. Returned evidence is always original source, with 1-based
    line numbers. Owners refer to verb owners, not the containing object's
    owner. Missing owners never satisfy wizard_owned.
    """
    if objs is None:
        objs = db.objects_with_programs() if isinstance(db, LazyDatabase) else list(all_objects(db))
    wizard_ids = {o.id for o in object_heads(db) if o.flags & ObjectFlags.WIZARD} if wizard_owned else set()
    seen = set()
    for o in objs:
        if o.id in seen:
            continue
        seen.add(o.id)
        for idx, v in enumerate(o.verbs):
            if owners is not None and int(v.owner) not in owners:
                continue
            if wizard_owned and int(v.owner) not in wizard_ids:
                continue
            if executable and not v.perms & VERB_EXEC:
                continue
            original = v.code or []
            lines = mask_moo_strings(original) if code_only else original
            if any(not any(rx.search(line) for line in lines) for rx in require):
                continue
            if any(rx.search(line) for rx in exclude for line in lines):
                continue
            for n, line in enumerate(lines, 1):
                if pattern.search(line):
                    yield GrepHit(o, idx, v, n, original[n - 1])


_MOO_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[A-Za-z_][A-Za-z0-9_]*|[0-9][A-Za-z0-9_]*|\.\.|\s+|.', re.S)
_MOO_KEYWORDS = frozenset({"if", "elseif", "else", "endif", "for", "in", "endfor", "fork", "endfork", "return",
                           "while", "endwhile", "try", "except", "finally", "endtry", "break", "continue", "any"})
_LABEL_KEYWORDS = frozenset({"while", "fork", "except"})  # `while NAME (`, `fork NAME (`, `except NAME (`
_CLOSERS = {"(": ")", "[": "]", "{": "}", "`": "'"}


@attrs.frozen
class BuiltinCall:
    """One call of a builtin function in verb code.

    ``args`` is the number of argument expressions written, with ``@`` appended
    when one of them is a ``@`` splice (the real count is known only when it runs).
    ``via`` is ``direct`` (``name(...)``), ``call_function`` (``call_function("name", ...)``
    with a literal name) or ``bf_wrapper`` (``X:bf_name(...)`` where ``bf_name`` is a
    wrapper verb on ``#0`` or its ancestors). ``in_wrapper`` marks calls made inside
    such a wrapper verb.
    """
    name: str
    obj: int
    index: int
    verb: str
    line: int  # 1-based
    args: str
    via: str
    in_wrapper: bool


def builtin_wrappers(db: MooDatabase) -> dict[str, tuple[int, int]]:
    """Builtin name -> (object, verb index) of each ``bf_<name>`` verb on ``#0`` or its ancestors.

    The server calls ``#0:bf_<name>`` (found by inheritance) for a protected
    builtin, so the first definition in ``db_ancestors()`` order wins.
    """
    system = db.objects.get(0)
    found: dict[str, tuple[int, int]] = {}
    if system is None:
        return found
    for o in db.ancestors(system):
        for idx, v in enumerate(o.verbs):
            for alias in v.name.split():
                if alias.lower().startswith("bf_") and len(alias) > 3:
                    found.setdefault(alias[3:].lower(), (o.id, idx))
    return found


def _string_literal(token: str) -> str:
    return re.sub(r'\\(.)', r'\1', token[1:-1], flags=re.S)


def builtin_calls_in_code(lines: Sequence[str], wrappers: Sequence[str] | set[str] = ()) -> Iterator[tuple[str, int, str, str]]:
    """Yield ``(name, line, args, via)`` for each builtin call in MOO source lines.

    A lexical scan following the grammar's rule that a builtin call is an
    identifier followed by ``(``: verb calls (after ``:`` or ``$``), property
    names (after ``.``), keywords and the labels of ``while``/``fork``/``except``
    are not calls. Arguments are counted at the top level of the call's
    parentheses; string contents are never read as code.
    """
    wrapper_names = {w.lower() for w in wrappers}
    prev: tuple[str, int] | None = None  # previous significant token and its line
    prev2: str | None = None
    line = 1
    stack: list[dict[str, Any]] = []
    for m in _MOO_TOKEN.finditer("\n".join(lines)):
        tok = m.group()
        if tok.isspace():
            line += tok.count("\n")
            continue
        top = stack[-1] if stack else None
        if top is not None and "name" in top and tok not in (")", ","):
            if tok == "@" and top["cur"] == 0:
                top["splice"] = True
            if top["argc"] == 0:
                top["first"].append(tok)
            top["cur"] += 1
        if tok in _CLOSERS:
            frame: dict[str, Any] = {"closer": _CLOSERS[tok]}
            if tok == "(" and prev is not None and (prev[0][0].isalpha() or prev[0][0] == "_"):
                word = prev[0].lower()
                if prev2 == ":" and word.startswith("bf_") and word[3:] in wrapper_names:
                    frame.update(name=word[3:], via="bf_wrapper")
                elif word not in _MOO_KEYWORDS and prev2 not in (":", "$", ".") and (prev2 or "").lower() not in _LABEL_KEYWORDS:
                    frame.update(name=word, via="direct")
                if "name" in frame:
                    frame.update(line=prev[1], argc=0, cur=0, splice=False, first=[])
            stack.append(frame)
        elif tok == "," and top is not None and "name" in top:
            top["argc"] += 1
            top["cur"] = 0
        elif stack and tok == stack[-1]["closer"]:
            done = stack.pop()
            if "name" in done:
                count = done["argc"] + (1 if done["cur"] or done["argc"] else 0)
                splice = "@" if done["splice"] else ""
                yield done["name"], done["line"], f"{count}{splice}", done["via"]
                first = done["first"]
                if done["name"] == "call_function" and len(first) == 1 and first[0].startswith('"'):
                    yield _string_literal(first[0]).lower(), done["line"], f"{count - 1}{splice}", "call_function"
        prev2 = prev[0] if prev is not None else None
        prev = (tok, line)


def builtin_calls(db: MooDatabase, objs: list[MooObject] | None = None) -> Iterator[BuiltinCall]:
    """Every builtin call in the verb code of objs (default: every object with code)."""
    wrappers = builtin_wrappers(db)
    wrapper_verbs = set(wrappers.values())
    if objs is None:
        objs = db.objects_with_programs() if isinstance(db, LazyDatabase) else list(all_objects(db))
    for o in objs:
        for idx, v in enumerate(o.verbs):
            inside = (o.id, idx) in wrapper_verbs
            for name, line, args, via in builtin_calls_in_code(v.code or [], wrappers):
                yield BuiltinCall(name, o.id, idx, v.name, line, args, via, inside)


def builtin_call_summary(calls: Iterator[BuiltinCall] | list[BuiltinCall], *, sites: bool = False) -> list[dict[str, Any]]:
    """One record per builtin name, in name order: call sites, verbs, objects, argument counts, routes."""
    grouped: dict[str, list[BuiltinCall]] = {}
    for c in calls:
        grouped.setdefault(c.name, []).append(c)
    result = []
    for name in sorted(grouped):
        group = grouped[name]
        record: dict[str, Any] = {
            "name": name,
            "sites": len(group),
            "verbs": len({(c.obj, c.index) for c in group}),
            "objects": len({c.obj for c in group}),
            "arg_counts": dict(sorted(Counter(c.args for c in group).items())),
            "via": dict(sorted(Counter(c.via for c in group).items())),
        }
        if sites:
            record["calls"] = [{"object": c.obj, "index": c.index, "verb": c.verb, "line": c.line,
                                "args": c.args, "via": c.via, "in_wrapper": c.in_wrapper} for c in group]
        result.append(record)
    return result


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


@attrs.frozen
class Cluster:
    root: MooObject
    members: list[MooObject]  # the root, then everything inside it, depth-first
    parents: Counter[int]  # parent -> how many members have it
    with_verbs: list[MooObject]  # members that define verbs
    definitions: Counter[str]  # property name -> how many members define it
    outside_children: list[tuple[MooObject, MooObject]]  # (member, its child that is not a member)
    players: list[MooObject]


@attrs.frozen
class InboundReference:
    member: int
    holder: MooObject  # an object outside every cluster
    where: str  # ".prop[2]<waif #1641>.dest" or ":[3] verbname:12"
    text: str  # the code line, or ""


_OBJECT_NUMBER = re.compile(r"(?<![\w#$-])#(\d+)(?![0-9])")


def clusters(db: MooDatabase, roots: list[MooObject]) -> tuple[list[Cluster], list[InboundReference]]:
    """Each root with everything inside it, what those objects define, and what outside them refers to them.

    "Outside" means outside every cluster asked for, so clusters that refer to
    each other can be judged together. Inbound references are property values
    holding a member's number (through lists, maps and waif bodies) and verb
    code naming ``#N``. A member's ``$name`` shows as #0's property holding it.
    """
    found = []
    union: set[int] = set()
    for root in roots:
        members = [root, *descendants(db, root, "contents")]
        ids = {m.id for m in members}
        union |= ids
        found.append(Cluster(
            root,
            members,
            Counter(int(p) for m in members for p in m.parents),
            [m for m in members if m.verbs],
            Counter(p.propertyName for m in members for p in own_properties(m)),
            [(m, db.objects[int(c)]) for m in members for c in m.children if int(c) not in ids and int(c) in db.objects],
            [m for m in members if m.flags & ObjectFlags.USER],
        ))
    inbound: list[InboundReference] = []

    def walk(value: Any, where: str, open_waifs: frozenset[int]) -> Iterator[tuple[int, str]]:
        if isinstance(value, ObjNum):
            if int(value) in union:
                yield int(value), where
        elif isinstance(value, list):
            for i, item in enumerate(value, 1):
                yield from walk(item, f"{where}[{i}]", open_waifs)
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from walk(key, f"{where}.keys[{_format(key)}]", open_waifs)
                yield from walk(item, f"{where}[{_format(key)}]", open_waifs)
        elif isinstance(value, WaifReference):
            waif = db.waifs.get(value.index)
            if waif is None or value.index in open_waifs:
                return
            names = waif_slot_names(db, int(waif.waif_class))
            for slot, item in waif.props:
                name = names[slot] if slot < len(names) else f"slot{slot}"
                yield from walk(item, f"{where}<waif #{int(waif.waif_class)}>.{name}", open_waifs | {value.index})

    for obj in db.objects.values():
        if obj.id in union:
            continue
        names = [p.propertyName for a in db.ancestors(obj) for p in own_properties(a)]
        for index, prop in enumerate(obj.properties):
            if prop.value is CLEAR:
                continue
            name = names[index] if index < len(names) else f"slot{index}"
            for member, where in walk(prop.value, f".{name}", frozenset()):
                inbound.append(InboundReference(member, obj, where, ""))
    programs = db.objects_with_programs() if isinstance(db, LazyDatabase) else list(all_objects(db))
    for o in programs:
        if o.id in union:
            continue
        for idx, v in enumerate(o.verbs):
            original = v.code or []
            # String literals, comments included, are masked: "Last modified … (#4913@Mongoose)" names no object.
            for n, line in enumerate(mask_moo_strings(original), 1):
                for number in {int(m) for m in _OBJECT_NUMBER.findall(line)} & union:
                    inbound.append(InboundReference(number, o, f":[{idx}] {v.name.split(' ')[0]}:{n}", original[n - 1].strip()))
    return found, inbound


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


class ValueStep(NamedTuple):
    """One step on the path from an object to a stored value."""

    kind: str  # prop, slot, index, key (into a map value), keyof (a map key itself)
    key: Any  # property or slot name, 1-based list index, or map key
    waif_class: int | None = None  # for a slot: the class of the waif entered


@attrs.frozen
class StoredValue:
    obj: MooObject  # the object whose property holds the value, directly or through waifs
    where: str  # ".prop[2]<waif #3010>.snd"; list indexes are 1-based like MOO
    value: Any
    steps: tuple[ValueStep, ...]  # the path as steps, the property first


def walk_stored_values(db: MooDatabase, skip: Callable[[str], bool] | None = None) -> Iterator[StoredValue]:
    """Every stored value and every value inside it, each container before its members.

    Walks each object's own (non-clear) property slots, anonymous objects
    included, through lists, map keys and values, and into waif bodies. A waif
    held in several places is walked once per holder. A waif that contains
    itself is entered once per path. A property or waif slot whose name
    ``skip`` accepts is not entered.
    """
    slot_names: dict[int, list[str]] = {}

    def walk(obj: MooObject, value: Any, where: str, steps: tuple[ValueStep, ...], open_waifs: frozenset[int]) -> Iterator[StoredValue]:
        yield StoredValue(obj, where, value, steps)
        if isinstance(value, list):
            for i, item in enumerate(value, 1):
                yield from walk(obj, item, f"{where}[{i}]", (*steps, ValueStep("index", i)), open_waifs)
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from walk(obj, key, f"{where}.keys[{_format(key)}]", (*steps, ValueStep("keyof", key)), open_waifs)
                yield from walk(obj, item, f"{where}[{_format(key)}]", (*steps, ValueStep("key", key)), open_waifs)
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
                if skip is not None and skip(name):
                    continue
                yield from walk(obj, item, f"{where}<waif #{cls}>.{name}", (*steps, ValueStep("slot", name, cls)), open_waifs | {value.index})

    for obj in db.objects.values():
        names = [p.propertyName for a in db.ancestors(obj) for p in own_properties(a)]
        for index, prop in enumerate(obj.properties):
            if prop.value is CLEAR:
                continue
            name = names[index] if index < len(names) else f"slot{index}"
            if skip is not None and skip(name):
                continue
            yield from walk(obj, prop.value, f".{name}", (ValueStep("prop", name),), frozenset())


def find_strings(db: MooDatabase, pattern: re.Pattern[str]) -> Iterator[StoredString]:
    """Every stored string matching ``pattern``, with the path to it (see ``walk_stored_values``)."""
    for v in walk_stored_values(db):
        if isinstance(v.value, str) and pattern.search(v.value):
            yield StoredString(v.obj, v.where, v.value, tuple(s.waif_class for s in v.steps if s.kind == "slot"))


# The keys a sound entry map may have (#2585:_snd_candidates lines 9-10).
SOUND_ENTRY_KEYS = frozenset({"group", "sound", "volume", "gain_db", "vary", "normalize", "modifiers"})
# Slots of a $catalog_sound waif holding catalog row ids, and the kind of row each names.
CATALOG_SLOTS = {"sound_id": "sound", "content_id": "content", "asset_id": "asset", "segment_id": "segment", "variation_group_id": "group"}
CATALOG_ID_KINDS = ("sound", "group", "content", "asset", "segment", "path", "slice")  # what an --ids line may name
CATALOG_REF_KINDS = (*CATALOG_ID_KINDS, "code")
_SOUND_FILE = re.compile(r"\.(m4a|ogg|wav|mp3|flac|aiff?|opus)(#\d+)?$", re.IGNORECASE)


@attrs.frozen
class CatalogRef:
    obj: MooObject  # the object holding the reference (for kind code, the verb's object)
    where: str  # path syntax of StoredString, or ":[idx] verb:line" for kind code
    kind: str  # one of CATALOG_REF_KINDS
    value: int | str
    context: str  # snd-property, snd-slot, social, snd-map, catalog-waif, string, code
    holder: str  # the innermost property or waif slot name on the path; for kind code, the verb's name


def _is_int(value: Any) -> bool:
    """A MOO INT: ObjNum and the other tagged scalars are not int, bool is excluded."""
    return isinstance(value, int) and not isinstance(value, bool)


def _sound_value_start(steps: tuple[ValueStep, ...], on_social: bool) -> tuple[int, str] | None:
    """Where a sound value begins on this path (index into ``steps``) and the context naming why, or None.

    A property or waif slot named ``*_snd`` holds one; on ``$social``, so do a
    social's ``["sound"]`` and each of its ``["sounds"][form]``
    (#1652:test_all_socials_populated lines 19-27).
    """
    for i, step in enumerate(steps):
        if step.kind in ("prop", "slot") and isinstance(step.key, str) and step.key.lower().endswith("_snd"):
            return i + 1, "snd-property" if step.kind == "prop" else "snd-slot"
    if on_social and len(steps) >= 2 and steps[1] == ValueStep("key", "sound"):
        return 2, "social"
    if on_social and len(steps) >= 3 and steps[1] == ValueStep("key", "sounds") and steps[2].kind == "key":
        return 3, "social"
    return None


def _sound_value_position(rest: tuple[ValueStep, ...]) -> str | None:
    """What a value at ``rest`` (the steps after a sound value's start) is in the grammar.

    ``"sound"``: an entry (the value itself, or one member of its list);
    ``"group"``/``"sound-key"``: an entry map's ``"group"``/``"sound"``;
    None: anywhere else, such as ``"volume"`` or the numbers of ``"vary"``.
    """
    if rest and rest[0].kind == "index":
        rest = rest[1:]
    if not rest:
        return "sound"
    if len(rest) == 1 and rest[0].kind == "key" and rest[0].key in ("group", "sound"):
        return "group" if rest[0].key == "group" else "sound-key"
    return None


def find_catalog_refs(db: MooDatabase, catalog_class: int | None, social: int | None, skip: Sequence[str] = (),
                      skipped: Counter | None = None) -> Iterator[CatalogRef]:
    """Every stored sound catalog reference, following the game's grammar of a sound value (#2585:_snd_candidates).

    A sound value is one entry or a list of entries; an entry is an INT > 0
    (a sound id), a STR (a path or slice name) or a map holding exactly one of
    ``"group"`` and ``"sound"``. Rules:

    1. A sound value is held in a property or waif slot named ``*_snd``, or on
       ``social`` under a social's ``"sound"`` or ``"sounds"[form]``.
    2. In a sound value, an INT > 0 entry is a sound; in an entry map an INT > 0
       under ``"group"`` is a group and under ``"sound"`` a sound.
    3. Elsewhere, a map whose keys are all sound entry keys and that holds an
       INT > 0 under ``"group"`` or ``"sound"`` gives that group or sound (snd-map).
    4. In a waif of ``catalog_class`` or a descendant, an INT > 0 in a
       ``CATALOG_SLOTS`` slot names that row; an object number there is unset.
    5. Any STR ending in a sound file extension is a path, or a slice with ``#N``.
    6. A property or waif slot whose name matches a ``skip`` glob is not
       entered; ``skipped`` counts them by name.
    """
    catalog = set()
    if catalog_class is not None and catalog_class in db.objects:
        catalog = {catalog_class, *(d.id for d in descendants(db, db.objects[catalog_class]))}

    def skip_name(name: str) -> bool:
        if any(fnmatchcase(name, g) for g in skip):
            if skipped is not None:
                skipped[name] += 1
            return True
        return False

    for v in walk_stored_values(db, skip_name if skip else None):
        value, steps = v.value, v.steps
        holder = next(s.key for s in reversed(steps) if s.kind in ("prop", "slot"))
        if isinstance(value, str):
            match = _SOUND_FILE.search(value)
            if match:
                yield CatalogRef(v.obj, v.where, "slice" if match.group(2) else "path", value, "string", holder)
            continue
        start = _sound_value_start(steps, v.obj.id == social)
        position = _sound_value_position(steps[start[0]:]) if start else None
        if _is_int(value) and value > 0:
            last = steps[-1]
            if position is not None:
                yield CatalogRef(v.obj, v.where, "group" if position == "group" else "sound", value, start[1], holder)
            elif last.kind == "slot" and last.waif_class in catalog and last.key in CATALOG_SLOTS:
                yield CatalogRef(v.obj, v.where, CATALOG_SLOTS[last.key], value, "catalog-waif", holder)
        elif isinstance(value, dict) and position != "sound" and value and set(value) <= SOUND_ENTRY_KEYS:
            for key in ("group", "sound"):
                if key in value and _is_int(value[key]) and value[key] > 0:
                    yield CatalogRef(v.obj, f"{v.where}[{_format(key)}]", key, value[key], "snd-map", holder)


_STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
_INT_LITERAL = re.compile(r"(?<![\w#.])\d+(?![\w.])")


def find_code_literals(db: MooDatabase, numbers: set[int], strings: set[str]) -> Iterator[CatalogRef]:
    """Verb code lines holding one of ``numbers`` as an integer literal or one of ``strings`` as a string literal.

    Integers are read with string literals (comments included) masked, and
    ``#N`` object numbers and decimals are not integers.
    """
    programs = db.objects_with_programs() if isinstance(db, LazyDatabase) else list(all_objects(db))
    for o in programs:
        for idx, v in enumerate(o.verbs):
            original = v.code or []
            name = v.name.split(" ")[0]
            for n, line in enumerate(mask_moo_strings(original), 1):
                where = f":[{idx}] {name}:{n}"
                for number in sorted({int(m) for m in _INT_LITERAL.findall(line)} & numbers):
                    yield CatalogRef(o, where, "code", number, "code", name)
                literals = {re.sub(r"\\(.)", r"\1", m) for m in _STRING_LITERAL.findall(original[n - 1])}
                for text in sorted(literals & strings):
                    yield CatalogRef(o, where, "code", text, "code", name)


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


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# suspend() with no timeout: the server stores INTNUM_MAX (toaststunt tasks.cc bf_suspend).
INDEFINITE_SUSPEND = 2**63 - 1


def format_time(seconds: int | None) -> str:
    """UTC time of Unix ``seconds``; raw seconds when no calendar date holds them.

    Not datetime.fromtimestamp: on Windows it refuses negative and far times,
    which a task suspended without a deadline can carry.
    """
    if seconds is None:
        return "-"
    if seconds == INDEFINITE_SUSPEND:
        return "until resumed"
    try:
        return (_EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%SZ")
    except OverflowError:
        return f"{seconds}s"


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
