import io
import json
import re
import socket
import threading

import pytest
from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.remote import CENSUS_BLOCK, Err, Obj, RemoteError, Session, census, parse_literal, rank, read_census


def test_parse_literal_reads_every_value_kind():
    text = '{1, -2, 3.5, #7, #-1, "a \\"q\\" \\\\ b", E_PERM, {}, {{}}, ["k" -> 1, 2 -> {#3}]}'
    assert parse_literal(text) == [1, -2, 3.5, Obj(7), Obj(-1), 'a "q" \\ b', Err("E_PERM"), [], [[]],
                                   {"k": 1, 2: [Obj(3)]}]


def test_parse_literal_rejects_trailing_text():
    with pytest.raises(ValueError):
        parse_literal("{1} 2")


class FakeMoo:
    """A telnet peer that answers each eval line with the lines REPLY(line) returns."""

    def __init__(self, reply):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.received = []
        self.reply = reply
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        conn, _ = self.server.accept()
        conn.sendall(b"Welcome\r\n")
        buffer = b""
        while True:
            data = conn.recv(65536)
            if not data:
                return
            buffer += data
            while b"\r\n" in buffer:
                line, buffer = buffer.split(b"\r\n", 1)
                line = line.decode("latin-1")
                self.received.append(line)
                for out in self.reply(line):
                    conn.sendall(out.encode("latin-1") + b"\r\n")


def marker(line):
    return re.search(r'"(@@moodb [0-9a-f]+ )"', line).group(1)


def session(fake):
    return Session("127.0.0.1", fake.port, timeout=5, greeting_quiet=0.1)


def test_eval_rows_collects_marked_lines_and_ignores_chatter():
    def reply(line):
        if not line.startswith(";;"):
            return []
        head = marker(line)
        return ["Somebody pages you.", head + "{#1, \"x\"}", head + "{#2, \"y\"}", head + "end 2"]

    fake = FakeMoo(reply)
    rows = session(fake).eval_rows('for o in [#1..#2] __emit({o, "x"}); endfor')
    assert rows == [[Obj(1), "x"], [Obj(2), "y"]]
    assert "__n = __n + 1; notify(player," in fake.received[-1]


def test_eval_rows_fails_when_a_line_was_lost():
    def reply(line):
        head = marker(line)
        return [head + "{1}", head + "end 2"]

    with pytest.raises(RemoteError, match="sent 2 rows, received 1"):
        session(FakeMoo(reply)).eval_rows("__emit(1); __emit(2);")


def test_eval_rows_raises_the_server_traceback():
    def reply(line):
        return ["#-1:Input to EVAL, line 3:  Permission denied", "... called from built-in function eval()",
                "(End of traceback)"]

    with pytest.raises(RemoteError, match="Permission denied"):
        session(FakeMoo(reply)).eval_rows("__emit(verbs(#1));")


def test_census_writes_rows_and_block_records(tmp_path):
    def reply(line):
        head = marker(line)
        first = int(re.search(r"\[#(\d+)\.\.", line).group(1))
        return [head + f'{{#{first}, "Generic Thing {first}", #2, #1, 1, 1, 0, 4, 3, 2, 9}}', head + "end 1"]

    out = io.StringIO()
    census(session(FakeMoo(reply)), 0, 9, 5, out, pause=0)
    path = tmp_path / "c.jsonl"
    path.write_text(out.getvalue(), encoding="ascii")
    rows, blocks = read_census(path)
    assert blocks == [(0, 4), (5, 9)]
    assert rows[5] == {"object": 5, "name": "Generic Thing 5", "owner": 2, "parent": 1, "readable": True,
                       "fertile": True, "player": False, "verbs": 4, "readable_verbs": 3, "properties": 2,
                       "children": 9}


def test_census_block_program_emits_one_row_per_object():
    program = CENSUS_BLOCK.format(first=10, last=20)
    assert program.startswith("for o in [#10..#20]")
    assert program.count("__emit(") == 1 and "}); endif endfor" in program


def record(n, name, parent, props, inherited=(), verbs=(), props_known=True, fertile=False):
    return {"object": n, "name": name, "owner": 2, "parent": parent,
            "flags": {"r": True, "f": fertile, "w": False, "player": False, "programmer": False, "wizard": False},
            "verb_names": [v["info"][2] for v in verbs], "verbs": list(verbs), "props_known": props_known,
            "props": list(props), "inherited": list(inherited)}


def prop(name, value, readable=True):
    return {"name": name, "info": [2, "rc"], "readable": readable, "value": value}


def test_built_database_reads_back_with_moodb(tmp_path):
    root = record(1, "Root Class", -1, [prop("description", "")])
    verb = {"index": 1, "info": [2, "rxd", "sit"], "args": ["any", "on top of/on/onto/upon", "this"],
            "lines": 4, "code": ['"sit on <seat>";', '"  takes a \\"seat\\"";', 'player:tell("You sit.");', "return 1;"]}
    stand = {"index": 2, "info": [2, "rx", "stand"], "args": ["this", "none", "none"], "lines": 0, "code": []}
    hidden = {"index": 3, "info": [2, "x", "count"], "args": ["this", "any", "any"], "lines": -1, "code": None}
    seat = record(5, "Generic Seat", 1, [prop("seats", 4), prop("secret", None, readable=False),
                                         prop("owner_list", [{"obj": 2}, {"err": "E_PERM"}, {"float": 1.5}])],
                  inherited=[{"from": 1, "name": "description", "info": [2, "rc"], "clear": 1,
                              "readable": True, "value": 0}], verbs=[verb, stand, hidden], fertile=True)
    orphan = record(9, "Child of the unread", 7, [])
    blind = record(11, "Child of the unreadable", 12, [])
    unread_props = record(12, "Hidden", 1, [], props_known=False)
    records_dir = tmp_path / "records"
    records_dir.mkdir()
    system = record(0, "The System Object", 1, [prop("seat", {"obj": 5})])
    for r in (system, root, seat, orphan, blind, unread_props):
        (records_dir / f"{r['object']}.json").write_text(json.dumps(r), encoding="ascii")
    out = tmp_path / "lambda.db"

    result = CliRunner().invoke(moodb, ["remote", "build", str(records_dir), str(out)])
    assert result.exit_code == 0, result.output
    assert f"{out}: 3 objects" in result.output
    assert "left out #9: property names of #7 not read" in result.output
    assert "left out #11: property names of #12 not read" in result.output
    assert "left out #12: property names of #12 not read" in result.output

    code = CliRunner().invoke(moodb, ["--db", str(out), "--no-cache", "code", "#5:sit"])
    assert code.exit_code == 0, code.output
    assert 'player:tell("You sit.");' in code.output and "return 1;" in code.output
    listed = CliRunner().invoke(moodb, ["--db", str(out), "--no-cache", "commands", "#5", "-i", "--lines", "1"])
    assert listed.exit_code == 0, listed.output
    assert listed.output.splitlines() == [
        "== #5 Generic Seat  (under #1 Root Class)",
        '  #5:[0] "sit"  any on top of/on/onto/upon this',
        "      sit on <seat>",
        '  #5:[1] "stand"  this none none',
        '  #5:[2] "count"  this any any',
    ]
    verbs = CliRunner().invoke(moodb, ["--db", str(out), "--no-cache", "verbs", "#5"])
    assert verbs.exit_code == 0, verbs.output
    assert "any on top of/on/onto/upon this" in verbs.output
    assert "this none none" in verbs.output and "this any any" in verbs.output
    props = CliRunner().invoke(moodb, ["--db", str(out), "--no-cache", "props", "#5"])
    assert props.exit_code == 0, props.output
    assert "seats" in props.output and "unreadable" in props.output and "#2" in props.output


def test_two_equal_verbs_on_one_object_write_as_two_programs(tmp_path):
    same = {"info": [2, "rxd", "look"], "args": ["this", "none", "none"], "lines": 1, "code": ["return 1;"]}
    records_dir = tmp_path / "records"
    records_dir.mkdir()
    for r in (record(0, "System", -1, []),
              record(4, "Twice", -1, [], verbs=[{"index": 1, **same}, {"index": 2, **same}])):
        (records_dir / f"{r['object']}.json").write_text(json.dumps(r), encoding="ascii")
    out = tmp_path / "x.db"
    assert CliRunner().invoke(moodb, ["remote", "build", str(records_dir), str(out)]).exit_code == 0
    code = CliRunner().invoke(moodb, ["--db", str(out), "--no-cache", "code", "#4:1"])
    assert code.exit_code == 0, code.output
    assert "return 1;" in code.output


def test_match_sorts_verbs_into_exact_similar_and_new(tmp_path):
    def db_of(records):
        d = tmp_path / f"r{len(list(tmp_path.iterdir()))}"
        d.mkdir()
        for r in records:
            (d / f"{r['object']}.json").write_text(json.dumps(r), encoding="ascii")
        out = d / "x.db"
        result = CliRunner().invoke(moodb, ["remote", "build", str(d), str(out)])
        assert result.exit_code == 0, result.output
        return out

    def verb(i, name, code):
        return {"index": i, "info": [2, "rxd", name], "args": ["this", "none", "this"], "lines": len(code), "code": code}

    sit = ['player:tell("You sit.");', "this.seated = {@this.seated, player};", "return 1;"]
    stand = ['player:tell("You stand.");', "this.seated = setremove(this.seated, player);", "return 1;"]
    ours = db_of([record(0, "System", -1, []),
                  record(4, "Our Seat", -1, [], verbs=[verb(1, "sit", ["  " + l for l in sit] + ['"Last modified Tue Oct  6 01:00:00 2026 UTC by x";']),
                                                      verb(2, "stand", stand[:2] + ['return 0;'])])])
    theirs = db_of([record(0, "System", -1, []),
                    record(7, "Their Seat", -1, [], verbs=[verb(1, "sit", sit), verb(2, "stand", stand),
                                                         verb(3, "bounce", ['player:tell("Boing.");'])])])

    result = CliRunner().invoke(moodb, ["--db", str(ours), "--no-cache", "match", str(theirs), "#7", "--verbs"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "#7 Their Seat: have a different version (exact 1, similar 1, new 1)",
        "    sit: exact  #4:sit",
        "    stand: similar  #4:stand 50%",
        "    bounce: new",
    ]


def rows_file(tmp_path, rows):
    path = tmp_path / "census.jsonl"
    base = {"owner": 2, "parent": 1, "readable": True, "player": False, "verbs": 5, "properties": 0}
    path.write_text("".join(json.dumps({**base, **r}) + "\n" for r in rows), encoding="ascii")
    return path


def test_rank_puts_fertile_first_then_readable_code_then_children(tmp_path):
    path = rows_file(tmp_path, [
        {"object": 2, "name": "Wizard", "fertile": False, "readable_verbs": 0, "children": 0},
        {"object": 10, "name": "toy", "fertile": False, "readable_verbs": 40, "children": 0},
        {"object": 11, "name": "Generic Bar", "fertile": True, "readable_verbs": 5, "children": 1},
        {"object": 12, "name": "Generic Seat", "fertile": True, "readable_verbs": 5, "children": 30},
        {"object": 13, "name": "Rindalin", "fertile": False, "readable_verbs": 50, "children": 0, "player": True},
    ])
    rows, _ = read_census(path)
    assert [r["object"] for r in rank(rows)] == [12, 11, 10]
    assert [r["object"] for r in rank(rows, fertile_only=True)] == [12, 11]
    assert [r["object"] for r in rank(rows, include_players=True)] == [12, 11, 13, 10]
    assert [r["object"] for r in rank(rows, exclude_under=[1])] == []
    assert [r["object"] for r in rank(rows, exclude_under=[12])] == [11, 10]
    assert [r["object"] for r in rank(rows, under=[12])] == [12]

    result = CliRunner().invoke(moodb, ["remote", "rank", str(path), "--name", "seat"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("#12      f verbs   5/5   kids    30  Generic Seat  (owner #2 Wizard, parent #1)")
