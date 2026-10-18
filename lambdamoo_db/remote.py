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


def rank(rows, fertile_only=False, include_players=False, min_verbs=1):
    """Objects worth reading, best first: fertile, then most readable verbs, then most children."""
    chosen = [r for r in rows.values()
              if r["readable_verbs"] >= min_verbs
              and (include_players or not r["player"])
              and (r["fertile"] or not fertile_only)]
    return sorted(chosen, key=lambda r: (not r["fertile"], -r["readable_verbs"], -r["children"], r["object"]))
