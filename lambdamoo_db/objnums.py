"""Every place a dump holds an object number, reached by one walk.

``remap(db, visit)`` calls ``visit(kind, number, where)`` for each slot and stores the number it returns.
``kind`` is ``"object"`` for a permanent object number and ``"anon"`` for an anonymous object's dump
number, which is a separate space: ToastStunt numbers anonymous objects after the permanent ones at each
write (db_objects.cc db_write_anonymous). ``where`` is a tuple naming the slot. Finding references and
renumbering use this one walk, so they cannot disagree about where numbers live.

``FIELDS`` declares how every field of every model class holds numbers and drives the walk; a test keeps
it complete. Object ids, ``total_objects`` and ``recycled_objects`` are positions, not references, and
are left to the caller.
"""

import re
from collections.abc import Callable
from typing import Any

from .database import (
    VM,
    Activation,
    Anon,
    InterruptedTask,
    MooDatabase,
    MooObject,
    ObjNum,
    Property,
    QueuedTask,
    SuspendedTask,
    Verb,
    Waif,
    WaifReference,
)

Visit = Callable[[str, int, tuple], int]

# number:   one object number (int or ObjNum)
# holder:   the number of the record's own object, permanent or anonymous
# value:    a MOO value, walked for ObjNum and Anon through lists and map keys and values
# code:     verb source lines; object literals as ToastStunt's lexer reads them
# records:  a list or dict of model records, each walked by its own entry here
# bi_func:  lines a built-in saved for a suspended call (objects.cc bf_*_write)
# waif:     a waif's (slot, value) pairs
# connections: "player listener" lines (server.cc write_active_connections)
# position: an object's place in the dump, left to the caller
# none:     holds no object number
FIELDS: dict[type, dict[str, str]] = {
    MooDatabase: {
        "versionstring": "none", "version": "none", "total_objects": "position", "total_verbs": "none",
        "total_players": "none", "clocks": "none", "objects": "records", "queuedTasks": "records",
        "suspendedTasks": "records", "interruptedTasks": "records", "waifs": "records", "players": "value",
        "recycled_objects": "position", "pending_values": "value", "connections": "connections",
        "connections_with_listeners": "none", "has_connections_section": "none", "line_ending": "none",
        "v4_dummy": "none",
    },
    MooObject: {
        "id": "position", "name": "none", "flags": "none", "owner": "number", "location": "value",
        "parents": "value", "parents_listed": "none", "children": "value", "last_move": "value",
        "contents": "value", "verbs": "records", "properties": "records", "propdefs_count": "none",
        "anon": "none", "v4_blank_line": "none", "v4_first_content": "number", "v4_neighbor": "number",
        "v4_first_child": "number", "v4_sibling": "number",
    },
    Verb: {"name": "none", "owner": "number", "perms": "none", "preps": "none", "object": "holder", "code": "code"},
    Property: {"propertyName": "none", "value": "value", "owner": "number", "perms": "none"},
    Waif: {"waif_class": "number", "owner": "number", "props": "waif", "propdefs_length": "none"},
    WaifReference: {"index": "none"},  # an index into db.waifs; the waif's body is walked there
    Activation: {
        "this": "number", "threaded": "none", "player": "number", "programmer": "number", "vloc": "number",
        "debug": "none", "verb": "none", "verbname": "none", "code": "code", "stack": "value",
        "unused1": "none", "unused2": "none", "unused3": "none", "unused4": "none", "temp_value": "value",
        "temp_this": "value", "temp_vloc": "value", "rtEnv": "value", "temp_end": "value", "pc": "none",
        "bi_func": "none", "error": "none", "bi_func_name": "none", "bi_func_data": "bi_func",
        "language_version": "none", "argstr": "none", "dobjstr": "none", "prepstr": "none", "iobjstr": "none",
    },
    VM: {"locals": "value", "stack": "records", "top": "none", "vector": "none", "funcId": "none",
         "maxStackframes": "none"},
    QueuedTask: {"firstLineno": "none", "id": "none", "st": "none", "unused": "none", "value": "value",
                 "activation": "records", "rtEnv": "value", "code": "code"},
    SuspendedTask: {"firstLineno": "none", "id": "none", "startTime": "none", "value": "value", "vm": "records"},
    InterruptedTask: {"id": "none", "status": "none", "vm": "records"},
}

# ToastStunt's lexer (parser.y yylex): strings with backslash escapes, /* */ comments, and `#`, an
# optional `-` and digits for an object literal. Anything else after `#` does not compile.
_CODE_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|/\*.*?\*/|#(-?\d*)', re.S)

# objects.cc: bf_move_write, bf_create_write (create and recreate), bf_recycle_write.
_BI_FUNC_LINES = [
    re.compile(r"bf_move data: what = (-?\d+), where = (-?\d+), position = -?\d+"),
    re.compile(r"bf_create data: oid = (-?\d+)"),
    re.compile(r"bf_recycle data: oid = (-?\d+), cont = 0"),
    re.compile(r"bf_call_function data: fname = \w+"),  # names the next line's function; no number
]
_CONNECTION = re.compile(r"(-?\d+) (-?\d+)")


def remap(db: MooDatabase, visit: Visit) -> None:
    """Replace every object number in ``db`` by ``visit(kind, number, where)``."""
    _Walk(visit).record(db, (), "object")


def _replace_numbers(pattern: re.Pattern[str], text: str, replace: Callable[[int, int], int]) -> str:
    """``text`` with each group of ``pattern``'s full match replaced by ``replace(group, number)``."""
    match = pattern.fullmatch(text)
    if match is None:
        raise ValueError(f"unrecognised line {text!r}")
    out, at = [], 0
    for group in range(1, (pattern.groups or 0) + 1):
        start, end = match.span(group)
        out += [text[at:start], str(replace(group, int(match[group])))]
        at = end
    return "".join(out) + text[at:]


class _Walk:
    def __init__(self, visit: Visit) -> None:
        self.visit = visit

    def record(self, rec: Any, where: tuple, holder: str) -> None:
        if isinstance(rec, MooObject):
            where, holder = (f"{'anon ' if rec.anon else '#'}{rec.id}",), "anon" if rec.anon else "object"
        for name, how in FIELDS[type(rec)].items():
            if how in ("none", "position"):
                continue
            old = getattr(rec, name)
            new = getattr(self, how)(old, where + (name,), holder) if how != "records" else self.records(old, where + (name,), holder)
            if new is not old:
                setattr(rec, name, new)

    def records(self, items: Any, where: tuple, holder: str) -> Any:
        if items is None:
            return None
        if isinstance(items, dict):
            for key, item in items.items():
                self.record(item, where + (key,), holder)
        elif isinstance(items, list):
            for i, item in enumerate(items):
                if item is not None:
                    label = (f".{item.propertyName}" if isinstance(item, Property)
                             else f":{i}" if isinstance(item, Verb) else i)
                    self.record(item, where[:-1] + (label,) if isinstance(item, (Property, Verb)) else where + (label,), holder)
        else:
            self.record(items, where, holder)
        return items

    def _number(self, kind: str, old: Any, where: tuple) -> Any:
        new = self.visit(kind, int(old), where)
        return old if new == int(old) else type(old)(new)

    def number(self, old: Any, where: tuple, holder: str) -> Any:
        return None if old is None else self._number("object", old, where)

    def holder(self, old: Any, where: tuple, holder: str) -> Any:
        return self._number(holder, old, where)

    def value(self, v: Any, where: tuple, holder: str = "object") -> Any:
        t = type(v)
        if t is ObjNum:
            return self._number("object", v, where)
        if t is Anon:
            return v if int(v) == -1 else self._number("anon", v, where)  # Anon(-1) is the null reference
        if t is list:
            out = None
            for i, item in enumerate(v):
                new = self.value(item, where + (i + 1,))
                if new is not item:
                    out = out or list(v)
                    out[i] = new
            return v if out is None else out
        if t is dict:
            pairs = [(self.value(k, where + (i + 1, "key")), self.value(item, where + (i + 1, "value")))
                     for i, (k, item) in enumerate(v.items())]
            if all(nk is k and nv is item for (nk, nv), (k, item) in zip(pairs, v.items())):
                return v
            out = dict(pairs)
            if len(out) != len(v):
                raise ValueError(f"renumbering merged two map keys at {where}")
            return out
        return v

    def code(self, lines: list[str] | None, where: tuple, holder: str) -> list[str] | None:
        if not lines:
            return lines
        text = "\n".join(lines)

        def literal(match: re.Match[str]) -> str:
            digits = match[1]
            if digits is None:
                return match[0]
            if digits in ("", "-"):
                raise ValueError(f"malformed object literal at {where}")
            line = text.count("\n", 0, match.start()) + 1
            return f"#{self.visit('object', int(digits), where + (line,))}"

        new = _CODE_TOKEN.sub(literal, text)
        return lines if new == text else new.split("\n")

    def bi_func(self, lines: list[str], where: tuple, holder: str) -> list[str]:
        out = []
        for line in lines:
            pattern = next((p for p in _BI_FUNC_LINES if p.fullmatch(line)), None)
            if pattern is None:
                raise ValueError(f"unrecognised built-in data {line!r} at {where}")
            out.append(_replace_numbers(pattern, line, lambda g, n: self.visit("object", n, where + (g,))))
        return lines if out == lines else out

    def waif(self, props: list, where: tuple, holder: str) -> list:
        out = None
        for i, prop in enumerate(props):
            slot, value = prop if isinstance(prop, tuple) else (i, prop)
            new = self.value(value, where + (slot,))
            if new is not value:
                out = out or list(props)
                out[i] = (slot, new) if isinstance(prop, tuple) else new
        return props if out is None else out

    def connections(self, lines: list[str], where: tuple, holder: str) -> list[str]:
        out = [_replace_numbers(_CONNECTION, line, lambda g, n: self.visit("object", n, where + (i, g)))
               for i, line in enumerate(lines)]
        return lines if out == lines else out
