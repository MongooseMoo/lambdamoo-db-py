"""Read a live MOO over its telnet port, politely: one connection, one command at a time.

A census walks object numbers in blocks on the server side and sends back one
summary row per valid object, so the whole database costs a few hundred evals.
"""

import json
import re
import secrets
import socket
import time
from dataclasses import dataclass

from .database import CLEAR, MooDatabase, MooError, MooObject, ObjNum, Property, Verb
from .enums import ObjectFlags
from .inspection import ERROR_NAMES

MARK = "@@moodb"


class RemoteError(Exception):
    pass


@dataclass(frozen=True)
class Obj:
    id: int

    def __str__(self):
        return f"#{self.id}"


@dataclass(frozen=True)
class Err:
    name: str


_TOKEN = re.compile(r'\s*(?:(-?\d+\.\d*(?:[eE][-+]?\d+)?|-?\d+[eE][-+]?\d+)|(-?\d+)|#(-?\d+)|"((?:[^"\\]|\\.)*)"|(E_[A-Z]+)|([{},\[\]]|->))')


def parse_literal(text):
    """Parse a MOO literal as printed by toliteral(): ints, floats, strings, objects, errors, lists, maps."""
    pos = 0

    def token():
        nonlocal pos
        match = _TOKEN.match(text, pos)
        if not match:
            raise ValueError(f"bad MOO literal at {pos}: {text[pos:pos + 40]!r}")
        pos = match.end()
        return match

    def value(match):
        flt, num, obj, string, err, punct = match.groups()
        if flt is not None:
            return float(flt)
        if num is not None:
            return int(num)
        if obj is not None:
            return Obj(int(obj))
        if string is not None:
            return re.sub(r"\\(.)", r"\1", string)
        if err is not None:
            return Err(err)
        if punct == "{":
            return sequence("}", list)
        if punct == "[":
            return sequence("]", dict)
        raise ValueError(f"unexpected {punct!r} at {match.start()}")

    def sequence(close, kind):
        items = []
        match = token()
        if match.group(6) == close:
            return kind(items) if kind is list else {}
        while True:
            item = value(match)
            if kind is dict:
                if token().group(6) != "->":
                    raise ValueError(f"expected -> at {pos}")
                item = (item, value(token()))
            items.append(item)
            punct = token().group(6)
            if punct == close:
                return kind(items)
            if punct != ",":
                raise ValueError(f"expected , or {close} at {pos}")
            match = token()

    result = value(token())
    if text[pos:].strip():
        raise ValueError(f"trailing text at {pos}: {text[pos:pos + 40]!r}")
    return result


class Session:
    """One telnet connection to a MOO. Lines that are not this session's markers are ignored."""

    def __init__(self, host, port, timeout=60.0, greeting_quiet=3.0):
        self.sock = socket.create_connection((host, port), timeout=15)
        self.timeout = timeout
        self.buffer = b""
        self.read_quiet(greeting_quiet)

    def send(self, line):
        self.sock.sendall(line.encode("latin-1") + b"\r\n")

    def read_quiet(self, quiet):
        """Collect lines until nothing arrives for QUIET seconds."""
        lines = []
        self.sock.settimeout(quiet)
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                self.buffer += data
        except socket.timeout:
            pass
        *done, self.buffer = self.buffer.split(b"\n")
        lines.extend(d.decode("latin-1").rstrip("\r") for d in done)
        return lines

    def lines(self):
        """Yield lines as they arrive, failing after self.timeout seconds of silence."""
        self.sock.settimeout(self.timeout)
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                yield line.decode("latin-1").rstrip("\r")
            data = self.sock.recv(65536)
            if not data:
                raise RemoteError("connection closed by the server")
            self.buffer += data

    def who(self):
        """The pre-login @who listing, read without connecting a character."""
        self.send("@who")
        return self.read_quiet(3)

    def login(self, name, password):
        self.send(f"connect {name} {password}")
        text = "\n".join(self.read_quiet(4))
        if "different password" in text or "does not exist" in text:
            raise RemoteError(f"login as {name} refused")
        return text

    def eval_rows(self, statements):
        """Run STATEMENTS and return the values they send with `__emit(EXPR);` statements.

        Each value goes out as one marked line and the run ends with a marked count, which is
        checked: a lost line (output buffer overflow) is an error, not a gap.
        """
        tok = secrets.token_hex(4)
        prefix = f'"{MARK} {tok} "'
        body = re.sub(r"__emit\((.*?)\);",
                      lambda m: f"__n = __n + 1; notify(player, {prefix} + toliteral({m.group(1)}));",
                      statements, flags=re.S)
        program = f';;__n = 0; {body} notify(player, {prefix} + "end " + tostr(__n));'
        self.send(program)
        rows, trace, head = [], [], f"{MARK} {tok} "
        for line in self.lines():
            if line.startswith(head):
                body = line[len(head):]
                if body.startswith("end "):
                    if int(body[4:]) != len(rows):
                        raise RemoteError(f"sent {body[4:]} rows, received {len(rows)}")
                    return rows
                rows.append(parse_literal(body))
            elif trace or line.startswith("#-1:Input to EVAL"):
                trace.append(line)
                if line == "(End of traceback)":
                    raise RemoteError("\n".join(trace))
            elif "Network buffer overflow" in line or "output to you have been lost" in line:
                raise RemoteError(line)

    def close(self):
        try:
            self.send("@quit")
        finally:
            self.sock.close()


# One row per valid object. A count of -1 means the object is unreadable to this character.
CENSUS_FIELDS = ["object", "name", "owner", "parent", "readable", "fertile", "player",
                 "verbs", "readable_verbs", "properties", "children"]

CENSUS_BLOCK = (
    'for o in [#{first}..#{last}] if (valid(o)) '
    'vs = `verbs(o) ! ANY => 0\'; nr = 0; '
    'if (typeof(vs) == LIST) for i in [1..length(vs)] '
    'if (index(`verb_info(o, i)[2] ! ANY => ""\', "r")) nr = nr + 1; endif endfor endif '
    'ps = `properties(o) ! ANY => 0\'; '
    '__emit({{o, o.name, o.owner, parent(o), o.r, o.f, is_player(o), '
    'typeof(vs) == LIST ? length(vs) | -1, nr, typeof(ps) == LIST ? length(ps) | -1, '
    'length(children(o))}}); '
    'endif endfor'
)


def census_row(values):
    row = dict(zip(CENSUS_FIELDS, values))
    for key in ("object", "owner", "parent"):
        row[key] = row[key].id
    for key in ("readable", "fertile", "player"):
        row[key] = bool(row[key])
    return row


def census(session, first, last, block, out, pause=1.0, progress=None):
    """Census objects FIRST..LAST in blocks of BLOCK, appending JSON lines to OUT (a text file)."""
    start = first
    while start <= last:
        end = min(start + block - 1, last)
        rows = session.eval_rows(CENSUS_BLOCK.format(first=start, last=end))
        for values in rows:
            out.write(json.dumps(census_row(values), ensure_ascii=True) + "\n")
        out.write(json.dumps({"block": [start, end], "rows": len(rows)}) + "\n")
        out.flush()
        if progress:
            progress(start, end, len(rows))
        start = end + 1
        if start <= last:
            time.sleep(pause)


def read_census(path):
    """Rows and the set of completed blocks from a census file."""
    rows, blocks = {}, []
    with open(path, encoding="ascii") as handle:
        for line in handle:
            record = json.loads(line)
            if "block" in record:
                blocks.append(tuple(record["block"]))
            else:
                rows[record["object"]] = record
    return rows, blocks


# Everything one character can read of one object, as tagged rows:
#   {"obj", o, name, owner, parent, r, f, w, player, programmer, wizard, verb names or 0}
#   {"verb", i, verb_info or 0, verb_args or 0, code lines or -1}, then {"code", i, lines} in chunks
#   {"props", whether properties(o) was readable}
#   {"prop", name, property_info or 0, readable, value}            properties defined on o
#   {"ival", ancestor, name, property_info or 0, clear, readable, value}   inherited slots, parent first
# When verbs(o) is unreadable, verbs are tried by index until E_VERBNF.
OBJECT_READ = (
    'o = #{obj}; vs = `verbs(o) ! ANY => 0\'; '
    '__emit({{"obj", o, o.name, o.owner, parent(o), o.r, o.f, o.w, is_player(o), o.programmer, o.wizard, vs}}); '
    'n = typeof(vs) == LIST ? length(vs) | {max_verbs}; '
    'for i in [1..n] info = `verb_info(o, i) ! ANY\'; '
    'if (info == E_VERBNF) break; elseif (typeof(info) == ERR) info = 0; endif '
    'args = `verb_args(o, i) ! ANY => 0\'; code = `verb_code(o, i) ! ANY => 0\'; '
    '__emit({{"verb", i, info, args, typeof(code) == LIST ? length(code) | -1}}); '
    'if (typeof(code) == LIST) for a in [0..(length(code) - 1) / {chunk}] '
    '__emit({{"code", i, code[a * {chunk} + 1..min(a * {chunk} + {chunk}, length(code))]}}); endfor endif '
    '$command_utils:suspend_if_needed(0); endfor '
    'ps = `properties(o) ! ANY => 0\'; __emit({{"props", typeof(ps) == LIST}}); '
    'if (typeof(ps) == LIST) for p in (ps) ok = 1; v = `o.(p) ! ANY => (ok = 0)\'; '
    '__emit({{"prop", p, `property_info(o, p) ! ANY => 0\', ok, v}}); $command_utils:suspend_if_needed(0); endfor endif '
    'a = parent(o); while (valid(a)) for p in (`properties(a) ! ANY => {{}}\') '
    'c = `is_clear_property(o, p) ! ANY => -1\'; ok = 1; v = c == 1 ? 0 | `o.(p) ! ANY => (ok = 0)\'; '
    '__emit({{"ival", a, p, `property_info(o, p) ! ANY => 0\', c, ok, v}}); $command_utils:suspend_if_needed(0); endfor '
    'a = parent(a); endwhile'
)


def read_object(session, obj, max_verbs=500, chunk=30):
    """One object as a dict of its header, verbs (with code where readable) and property slots."""
    rows = session.eval_rows(OBJECT_READ.format(obj=obj, max_verbs=max_verbs, chunk=chunk))
    head = rows[0]
    record = {"object": head[1].id, "name": head[2], "owner": head[3].id, "parent": head[4].id,
              "flags": {"r": head[5], "f": head[6], "w": head[7], "player": head[8],
                        "programmer": head[9], "wizard": head[10]},
              "verb_names": head[11] if isinstance(head[11], list) else None,
              "verbs": [], "props_known": False, "props": [], "inherited": []}
    verbs = {}
    for row in rows[1:]:
        kind = row[0]
        if kind == "verb":
            verbs[row[1]] = {"index": row[1], "info": _plain(row[2]), "args": _plain(row[3]),
                             "lines": row[4], "code": [] if row[4] >= 0 else None}
            record["verbs"].append(verbs[row[1]])
        elif kind == "code":
            verbs[row[1]]["code"].extend(row[2])
        elif kind == "props":
            record["props_known"] = bool(row[1])
        elif kind == "prop":
            record["props"].append({"name": row[1], "info": _plain(row[2]), "readable": bool(row[3]),
                                    "value": _encode(row[4])})
        elif kind == "ival":
            record["inherited"].append({"from": row[1].id, "name": row[2], "info": _plain(row[3]),
                                        "clear": row[4], "readable": bool(row[5]), "value": _encode(row[6])})
    for verb in record["verbs"]:
        if verb["code"] is not None and len(verb["code"]) != verb["lines"]:
            raise RemoteError(f"#{obj} verb {verb['index']}: {verb['lines']} lines sent, {len(verb['code'])} received")
    return record


def _plain(value):
    """verb_info/verb_args/property_info result as JSON, or None where it was unreadable."""
    if not isinstance(value, list):
        return None
    return [v.id if isinstance(v, Obj) else v for v in value]


def _encode(value):
    """A MOO value as JSON that keeps its type: objects and errors are tagged."""
    if isinstance(value, Obj):
        return {"obj": value.id}
    if isinstance(value, Err):
        return {"err": value.name}
    if isinstance(value, float):
        return {"float": value}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {"map": [[_encode(k), _encode(v)] for k, v in value.items()]}
    return value


UNREADABLE = "<<moodb remote: value unreadable to the reading character>>"

PREPS = ["with/using", "at/to", "in front of", "in/inside/into", "on top of/on/onto/upon",
         "out of/from inside/from", "over", "through", "under/underneath/beneath", "behind", "beside",
         "for/about", "is", "as", "off/off of"]
ARG_SPECS = {"none": 0, "any": 1, "this": 2}


def _prep_index(prep):
    if prep == "none":
        return -2
    if prep == "any":
        return -1
    words = set(prep.split("/"))
    for index, spelling in enumerate(PREPS):
        if words & set(spelling.split("/")):
            return index
    raise ValueError(f"unknown preposition {prep!r}")


def _perm_bits(text, letters):
    return sum(bit for letter, bit in letters.items() if letter in text)


def _decode(value):
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        if "obj" in value:
            return ObjNum(value["obj"])
        if "err" in value:
            return MooError(ERROR_NAMES.index(value["err"]))
        if "float" in value:
            return float(value["float"])
        return {_decode(k): _decode(v) for k, v in value["map"]}
    return value


def build_database(records):
    """A version 17 MooDatabase of the read objects; numbers not read are recycled slots.

    Returns (db, skipped): an object's property slots need the property names of it and every
    ancestor, so an object where any of those was not read, or was unreadable, is left out and
    listed in skipped with the first such object.
    """
    def missing_ancestor(record):
        if not record["props_known"]:
            return record["object"]
        parent = record["parent"]
        while parent >= 0:
            if parent not in records or not records[parent]["props_known"]:
                return parent
            parent = records[parent]["parent"]
        return None

    skipped = {n: missing_ancestor(r) for n, r in records.items() if missing_ancestor(r) is not None}
    kept = {n: r for n, r in records.items() if n not in skipped}
    db = MooDatabase()
    db.version = 17
    db.versionstring = "** LambdaMOO Database, Format Version 17 **"
    for n, record in kept.items():
        flags = record["flags"]
        bits = ((ObjectFlags.USER if flags["player"] else 0) | (ObjectFlags.PROGRAMMER if flags["programmer"] else 0)
                | (ObjectFlags.WIZARD if flags["wizard"] else 0) | (ObjectFlags.READ if flags["r"] else 0)
                | (ObjectFlags.WRITE if flags["w"] else 0) | (ObjectFlags.FERTILE if flags["f"] else 0))
        obj = MooObject(n, record["name"], bits, record["owner"], -1,
                        [record["parent"]] if record["parent"] >= 0 else [])
        names = record["verb_names"]
        for verb in record["verbs"]:
            info, args = verb["info"], verb["args"]
            name = info[2] if info else (names[verb["index"] - 1] if names else f"<<unreadable verb {verb['index']}>>")
            perms = _perm_bits(info[1], {"r": 1, "w": 2, "x": 4, "d": 8}) if info else 0
            preps = -2
            if args:
                perms |= ARG_SPECS[args[0]] << 4 | ARG_SPECS[args[2]] << 6
                preps = _prep_index(args[1])
            v = Verb(name, info[0] if info else -1, perms, preps, n)
            v.code = verb["code"]
            obj.verbs.append(v)

        def slot(name, info, readable, value, clear=False):
            owner, perms = (info[0], _perm_bits(info[1], {"r": 1, "w": 2, "c": 4})) if info else (-1, 0)
            return Property(name, CLEAR if clear else _decode(value) if readable else UNREADABLE, owner, perms)

        obj.properties = [slot(p["name"], p["info"], p["readable"], p["value"]) for p in record["props"]]
        obj.propdefs_count = len(obj.properties)
        for parent in _chain(kept, record["parent"]):
            values = {i["name"]: i for i in record["inherited"] if i["from"] == parent}
            for p in kept[parent]["props"]:
                i = values.get(p["name"])
                obj.properties.append(slot(p["name"], i and i["info"], bool(i and i["readable"]),
                                           i and i["value"], clear=bool(i and i["clear"] == 1)))
        db.objects[n] = obj
    for n, obj in db.objects.items():
        if obj.parents:
            db.objects[obj.parents[0]].children.append(n)
    db.total_objects = max(db.objects, default=-1) + 1
    db.recycled_objects = set(range(db.total_objects)) - set(db.objects)
    db.players = sorted(n for n, r in kept.items() if r["flags"]["player"])
    return db, skipped


def _chain(records, parent):
    while parent >= 0:
        yield parent
        parent = records[parent]["parent"]


def rank(rows, fertile_only=False, include_players=False, min_verbs=1):
    """Objects worth reading, best first: fertile, then most readable verbs, then most children."""
    chosen = [r for r in rows.values()
              if r["readable_verbs"] >= min_verbs
              and (include_players or not r["player"])
              and (r["fertile"] or not fertile_only)]
    return sorted(chosen, key=lambda r: (not r["fertile"], -r["readable_verbs"], -r["children"], r["object"]))
