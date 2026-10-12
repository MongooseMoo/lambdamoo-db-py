from pathlib import Path

import pytest
from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum, Property, Verb
from lambdamoo_db.lazy import open_indexed
from lambdamoo_db.reader import load
from lambdamoo_db.verb_changes import changed_objects, verb_changes, verb_holders
from lambdamoo_db.writer import dump

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"
STAMP_A = '"Last modified Fri Oct  9 10:00:00 2026 UTC by A (#2).";'
STAMP_B = '"Last modified Sat Oct 10 11:00:00 2026 UTC by B (#5).";'
RULE = "=" * 72


def _db(verbs_by_obj) -> MooDatabase:
    """Objects by number, each a list of (names, code) verbs: `this none this`, rxd, owned by #2. #0 is always there."""
    db = MooDatabase()
    for num, verbs in {0: [], **verbs_by_obj}.items():
        o = MooObject(num, f"obj{num}", 0, 0, -1, [])
        for name, code in verbs:
            v = Verb(name, ObjNum(2), 173, -1, num)
            v.code = code
            o.verbs.append(v)
        db.objects[num] = o
    return db


def _stamp(db: MooDatabase, times: dict[int, int]) -> MooDatabase:
    for num, when in times.items():
        db.objects[num].properties = [Property("creation_time", when, 0, 5)]
        db.objects[num].propdefs_count = 1
    return db


def _kinds(before, after):
    return [(c.kind, (c.new or c.old).name, c.old_index, c.new_index) for c in verb_changes(before, after)]


def _reading_dbs():
    """#1 has one verb of each kind: added, source changed, only metadata changed, removed, and one untouched."""
    old = _db({1: [
        ("keep", ["return 1;"]),
        ("edit", ['a = "x" && b < 2;', "old();", "return a;", STAMP_A]),
        ("gone", ["bye();"]),
        ("meta", ["m();"]),
    ]})
    new = _db({1: [
        ("fresh", ['notify(player, "a\\b" + tostr(1 && 2));', STAMP_B]),
        ("keep", ["return 1;"]),
        ("edit", ['a = "x" && b < 2;', "new();", "return a;", STAMP_B]),
        ("meta", ["m();"]),
    ]})
    meta = new.objects[1].verbs[3]
    meta.perms ^= 8  # the d bit
    meta.owner = ObjNum(5)
    return old, new


def test_added_changed_removed_and_metadata_only_verbs_are_told_apart():
    old, new = _reading_dbs()
    assert _kinds(old.objects[1], new.objects[1]) == [
        ("added", "fresh", None, 0), ("changed", "edit", 1, 2), ("changed", "meta", 3, 3), ("removed", "gone", 2, None),
    ]
    edit, meta = verb_changes(old.objects[1], new.objects[1])[1:3]
    assert edit.source_changed and edit.metadata == []
    assert not meta.source_changed and meta.metadata == ["perms: rxd -> rx", "owner: #2 -> #5"]
    # An object only one dump has: every verb is added, or removed.
    assert _kinds(None, new.objects[1])[0] == ("added", "fresh", None, 0)
    assert {kind for kind, *_ in _kinds(old.objects[1], None)} == {"removed"}
    assert verb_changes(old.objects[1], old.objects[1]) == []


def test_a_renamed_or_respecified_verb_is_one_changed_verb():
    old = _db({1: [("look", ["a();", "b();"]), ("take", ["t();"]), ("same", ["1;"]), ("same", ["2;"])]})
    new = _db({1: [("look l*ook", ["a();", "b();"]), ("take", ["t();", "u();"]), ("same", ["1;"]), ("same", ["3;"]), ("same", ["4;"])]})
    new.objects[1].verbs[1].preps = 0  # this with/using this
    changes = verb_changes(old.objects[1], new.objects[1])
    assert [(c.kind, c.new.name, c.new_index) for c in changes] == [
        ("changed", "look l*ook", 0), ("changed", "take", 1), ("changed", "same", 3), ("added", "same", 4),
    ]
    assert changes[0].metadata == ['names: "look" -> "look l*ook"'] and not changes[0].source_changed
    assert changes[1].metadata == ["args: this none this -> this with/using this"] and changes[1].source_changed
    # A program that was never there differs from an empty one.
    old, new = _db({1: [("stub", None)]}), _db({1: [("stub", [])]})
    assert verb_changes(old.objects[1], new.objects[1])[0].metadata == ["program: no program -> 0 lines"]


def test_a_new_last_modified_line_alone_still_counts_as_a_change_and_is_named():
    old = _db({1: [("saved", ["x();", STAMP_A]), ("edited", ["x();", STAMP_A])]})
    new = _db({1: [("saved", ["x();", STAMP_B]), ("edited", ["y();", STAMP_B])]})
    saved, edited = verb_changes(old.objects[1], new.objects[1])
    assert saved.kind == "changed" and saved.only_trailer
    assert edited.kind == "changed" and not edited.only_trailer
    entry, = changed_objects(old, new)
    assert (entry.count("changed"), entry.only_trailer) == (2, 1)


def _patch(monkeypatch, tmp_path, old, new):
    import lambdamoo_db.cli as cli

    old_path, new_path = tmp_path / "old.db", tmp_path / "new.db"
    for path in (old_path, new_path):
        path.write_text("")
    monkeypatch.setattr(cli, "open_indexed", lambda path, *_: old if Path(path) == old_path else new)
    monkeypatch.setattr(cli, "load", lambda path: old if Path(path) == old_path else new)

    def run(*args):
        return CliRunner().invoke(moodb, ["--db", str(new_path), "diff", str(old_path), "--plain", *args])

    return run


def test_cli_plain_writes_source_as_it_is(monkeypatch, tmp_path):
    run = _patch(monkeypatch, tmp_path, *_reading_dbs())
    out = tmp_path / "out"
    r = run("--output-dir", str(out))
    assert r.exit_code == 1, r.output
    assert r.output == f"different: plain {out}\n"
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == ["index.txt", "objects/permanent-1.txt"]
    assert (out / "index.txt").read_text(encoding="utf-8").splitlines() == [
        "Verb changes from OLD old.db to NEW new.db",
        "Context: 3 lines",
        "Objects: all",
        "1 objects: 1 added, 2 changed, 1 removed verbs",
        "Each object is in objects/permanent-N.txt. [N] after a verb is its 0-based position on the object.",
        "",
        " added  changed  removed  object",
        '     1        2        1  #1 "obj1"  [by number only]',
    ]
    assert (out / "objects" / "permanent-1.txt").read_text(encoding="utf-8").splitlines() == [
        '#1 "obj1"',
        "Verb changes from OLD old.db to NEW new.db",
        "1 added, 2 changed, 1 removed verbs",
        "Object: in both dumps by number only (no .creation_time to compare)",
        "",
        RULE,
        "ADDED  #1:fresh  [0]",
        'names "fresh"   args this none this   perms rxd   owner #2',
        RULE,
        '     1  notify(player, "a\\b" + tostr(1 && 2));',
        "     2  " + STAMP_B,
        "",
        RULE,
        "CHANGED  #1:edit  [2]",
        'names "edit"   args this none this   perms rxd   owner #2',
        "source: +2 -2 lines",
        RULE,
        "@@ -1,4 +1,4 @@",
        ' a = "x" && b < 2;',
        "-old();",
        "+new();",
        " return a;",
        "-" + STAMP_A,
        "+" + STAMP_B,
        "",
        RULE,
        "CHANGED  #1:meta  [3]",
        'names "meta"   args this none this   perms rx   owner #5',
        "perms: rxd -> rx",
        "owner: #2 -> #5",
        "source unchanged (1 lines)",
        RULE,
        "",
        RULE,
        "REMOVED  #1:gone  [2]",
        'names "gone"   args this none this   perms rxd   owner #2',
        RULE,
        "     1  bye();",
    ]
    # Without --output-dir the same text goes to standard output, index first.
    r = run()
    assert r.exit_code == 1, r.output
    assert r.output.startswith("Verb changes from OLD old.db to NEW new.db\nContext: 3 lines\n")
    assert '\n#1 "obj1"\n' in r.output and "\n-old();\n+new();\n" in r.output


def test_cli_plain_never_truncates_and_shows_context_or_the_whole_verb(monkeypatch, tmp_path):
    body = [f"line{n}();" for n in range(1, 401)]
    old = _db({1: [("long", body + [STAMP_A])]})
    new = _db({1: [("long", body[:199] + ["changed();"] + body[200:] + [STAMP_A]), ("huge", body)]})
    run = _patch(monkeypatch, tmp_path, old, new)
    text = run().output
    assert "   400  line400();" in text and text.count("line1();") == 1
    assert "\n@@ -197,7 +197,7 @@\n line197();\n line198();\n line199();\n-line200();\n+changed();\n line201();" in text
    # The Last modified line did not change and is outside the hunk; it is still shown.
    assert f"\nLast modified line unchanged: {STAMP_A}\n" in text
    assert "\n@@ -200,1 +200,1 @@\n-line200();\n+changed();\n\n" in run("--context", "0").output
    full = run("--full", "-o", "#1").output
    assert "\n   199  line199();\n-       line200();\n+  200  changed();\n   201  line201();\n" in full
    assert f"\n   401  {STAMP_A}\n" in full and "@@" not in full


def _continuity_dbs():
    """#1 is one object in both dumps, #2 a number recycled and reused, #3 new, #4 unstamped, #5 gone, #6 untouched."""
    old = _stamp(_db({n: [("say", ["a();"])] for n in (1, 2, 4, 5, 6)}), {1: 100, 2: 200, 5: 500, 6: 600})
    new = _stamp(_db({**{n: [("say", ["a();", "b();"])] for n in (1, 2, 3, 4)}, 6: [("say", ["a();"])]}), {1: 100, 2: 999, 3: 999, 6: 600})
    return old, new


def test_changed_objects_come_in_number_order_with_how_each_relates_to_old():
    old, new = _continuity_dbs()
    found = changed_objects(old, new)
    assert [(e.num, e.state, e.count("added"), e.count("changed"), e.count("removed")) for e in found] == [
        (1, "same", 0, 1, 0), (2, "reused", 0, 1, 0), (3, "absent", 1, 0, 0), (4, "unproven", 0, 1, 0), (5, "gone", 0, 0, 1),
    ]
    assert found[4].label == '#5 "obj5"'
    assert [e.num for e in changed_objects(old, new, (4, 1, 6))] == [1, 4]


def test_cli_plain_filters_choose_the_objects_and_the_index_says_so(monkeypatch, tmp_path):
    run = _patch(monkeypatch, tmp_path, *_continuity_dbs())

    def index(*args):
        out = tmp_path / f"out{len(list(tmp_path.iterdir()))}"
        r = run("--output-dir", str(out), *args)
        assert r.exit_code == 1, r.output
        files = sorted(int(p.stem.split("-")[1]) for p in (out / "objects").iterdir())
        return files, (out / "index.txt").read_text(encoding="utf-8").splitlines()

    files, lines = index()
    assert files == [1, 2, 3, 4, 5]
    assert lines[2:4] == ["Objects: all", "5 objects: 1 added, 3 changed, 1 removed verbs"]
    assert lines[-5:] == [
        '     0        1        0  #1 "obj1"',
        '     0        1        0  #2 "obj2"  [reused number]',
        '     1        0        0  #3 "obj3"  [new object]',
        '     0        1        0  #4 "obj4"  [by number only]',
        '     0        0        1  #5 "obj5"  [removed object]',
    ]

    files, lines = index("-o", "#2", "-o", "#3", "-o", "#6")
    assert files == [2, 3]
    assert lines[2:4] == ["Objects: #2 #3 #6", "2 objects: 1 added, 1 changed, 0 removed verbs"]

    files, lines = index("--exclude", "#1", "--exclude", "#5")
    assert files == [2, 3, 4]
    assert lines[2:5] == ["Objects: all", "Excluded: #1 #5", "3 objects: 1 added, 2 changed, 0 removed verbs"]
    assert lines[-4:] == [
        "Left out by --exclude: 2 objects, 0 added, 1 changed, 1 removed verbs",
        " added  changed  removed  object",
        '     0        1        0  #1 "obj1"',
        '     0        0        1  #5 "obj5"  [removed object]',
    ]

    # --existing means what it means on `added`: no new numbers, no reused ones; a removed object was OLD's.
    files, lines = index("--existing")
    assert files == [1, 4, 5]
    assert lines[3:5] == ["Only objects OLD already had (--existing, told apart by .creation_time)",
                          "3 objects: 0 added, 2 changed, 1 removed verbs"]
    assert lines[-4:] == [
        "Left out by --existing: 2 objects, 1 added, 1 changed, 0 removed verbs",
        " added  changed  removed  object",
        '     0        1        0  #2 "obj2"  [reused number]',
        '     1        0        0  #3 "obj3"  [new object]',
    ]
    # With no such property nothing can be called reused: only the new number goes.
    files, _ = index("--existing", "--identity", "born")
    assert files == [1, 2, 4, 5]
    files, lines = index("--existing", "--exclude", "#4", "-o", "#3", "-o", "#4", "-o", "#5")
    assert files == [5]
    assert [line for line in lines if line.startswith("Left out")] == [
        "Left out by --exclude: 1 objects, 0 added, 1 changed, 0 removed verbs",
        "Left out by --existing: 1 objects, 1 added, 0 changed, 0 removed verbs",
    ]


def test_cli_plain_exit_status_and_refusals(monkeypatch, tmp_path):
    old, new = _continuity_dbs()
    run = _patch(monkeypatch, tmp_path, old, new)
    out = tmp_path / "out"
    r = run("-o", "#6", "--output-dir", str(out))
    assert r.exit_code == 0 and r.output == f"equal: plain {out}\n"
    assert not (out / "objects").exists()
    r = run("--output-dir", str(out))
    assert r.exit_code == 2 and "already exists" in r.output
    r = run("-o", "#999")
    assert r.exit_code == 2 and "absent on both sides" in r.output
    for options in (["--format", "jsonl"], ["--view", "world"], ["--kind", "added"], ["--max-events", "5"]):
        r = run(*options)
        assert r.exit_code == 2 and "--plain shows verb changes only" in r.output, r.output
    assert run("--view", "code").exit_code == 1
    for option in (["--existing"], ["--exclude", "#1"], ["--full"], ["--identity", "born"]):
        r = CliRunner().invoke(moodb, ["--db", str(tmp_path / "new.db"), "diff", str(tmp_path / "old.db"), *option])
        assert r.exit_code == 2 and f"{option[0]} needs --plain" in r.output, r.output


def test_plain_reads_real_dumps_the_same_with_and_without_the_index(tmp_path):
    old_path, new_path = tmp_path / "old.db", tmp_path / "new.db"
    old_path.write_bytes(TOASTCORE.read_bytes())
    full = load(str(old_path))
    programmed = next(o for o in full.objects.values() if any(v.code for v in o.verbs))
    next(v for v in programmed.verbs if v.code).code.append('notify(player, "plain <regression> & more");')
    # A definition with no program, on an object with none: the index of programs does not list it.
    bare = next(o for o in full.objects.values() if not o.verbs)
    bare.verbs.append(Verb("unprogrammed", ObjNum(2), 173, -1, bare.id))
    with new_path.open("w", encoding="latin-1", newline="") as output:
        dump(full, output)

    def files(*options):
        out = tmp_path / f"out{len(list(tmp_path.iterdir()))}"
        r = CliRunner().invoke(moodb, ["--db", str(new_path), *options, "diff", str(old_path), "--plain", "--output-dir", str(out)])
        assert r.exit_code == 1, r.output
        return {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file()}

    whole = files("--no-cache")
    assert set(whole) == {"index.txt", f"objects/permanent-{programmed.id}.txt", f"objects/permanent-{bare.id}.txt"}
    assert '\n+notify(player, "plain <regression> & more");\n' in whole[f"objects/permanent-{programmed.id}.txt"]
    assert "(no program)" in whole[f"objects/permanent-{bare.id}.txt"]
    for _ in range(2):
        assert files("--cache-dir", str(tmp_path / "cache")) == whole

    lazy = open_indexed(new_path, tmp_path / "cache")
    holders = verb_holders(lazy)
    assert not lazy.objects.loaded
    assert {num: [(v.name, v.code) for v in o.verbs] for num, o in holders.items()} == {
        num: [(v.name, v.code) for v in o.verbs] for num, o in verb_holders(load(str(new_path))).items()}
