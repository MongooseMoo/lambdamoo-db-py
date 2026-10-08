import os
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import ObjNum, Property, Waif, WaifReference
from lambdamoo_db.lazy import LazyDatabase, build_index, index_path_for, open_indexed
from lambdamoo_db.reader import load
from lambdamoo_db.writer import dump

PROJECT_ROOT = Path(__file__).parent.parent
TOASTCORE = PROJECT_ROOT / "toastcore.db"
# toast2.db has an anonymous object, a waif, a recycled object and suspended tasks.
V17_DUMPS = [PROJECT_ROOT / "tests" / "fixtures" / "TypedMapKeys.db", TOASTCORE, PROJECT_ROOT / "toast2.db"]
HEADER_FIELDS = ("versionstring", "version", "total_objects", "total_verbs", "total_players", "clocks", "queuedTasks",
                 "suspendedTasks", "interruptedTasks", "players", "recycled_objects", "pending_values", "connections",
                 "connections_with_listeners", "line_ending")


@pytest.mark.parametrize("path", V17_DUMPS, ids=lambda p: p.name)
def test_each_object_alone_equals_the_full_parse(path):
    full = load(str(path))
    index, _ = build_index(path)
    assert list(LazyDatabase(path, index).objects) == list(full.objects)
    # A fresh database per object, newest first, so nothing it needs was loaded for an earlier one.
    for oid in reversed(list(full.objects)):
        lazy = LazyDatabase(path, index)
        assert lazy.objects[oid] == full.objects[oid]


@pytest.mark.parametrize("path", V17_DUMPS, ids=lambda p: p.name)
def test_header_waifs_and_whole_database_equal_the_full_parse(path):
    full = load(str(path))
    index, parsed = build_index(path)
    lazy = LazyDatabase(path, index)
    for field in HEADER_FIELDS:
        assert getattr(lazy, field) == getattr(full, field), field
    assert len(lazy.objects) == len(full.objects)
    assert len(lazy.waifs) == len(full.waifs)
    assert dict(lazy.waifs) == full.waifs
    assert dict(lazy.objects) == full.objects
    assert 10**9 not in lazy.objects and lazy.objects.get(10**9) is None
    fresh = LazyDatabase(path, index)
    assert fresh.object_names() == full.object_names()
    assert fresh.name_of(0) == full.objects[0].name and fresh.name_of(10**9) is None
    assert not fresh.objects.loaded  # names come from the index, not from records
    # The parse that builds the index is itself a complete database.
    assert parsed.objects == full.objects and parsed.waifs == full.waifs


def test_a_waif_defined_in_another_object_is_found(tmp_path):
    db = load(str(V17_DUMPS[0]))
    db.waifs[0] = Waif(ObjNum(7), ObjNum(3), [(0, "inside the waif")], 1)
    db.objects[0].properties[0].value = WaifReference(0)  # written first: the definition
    db.objects[2].properties.append(Property("held", WaifReference(0), ObjNum(3), 0))  # written later: a reference
    db.objects[2].propdefs_count = 1
    path = tmp_path / "waifs.db"
    with open(path, "w", encoding="latin-1", newline="") as f:
        dump(db, f)
    full = load(str(path))
    assert full.waifs[0].props == [(0, "inside the waif")]

    index, _ = build_index(path)
    lazy = LazyDatabase(path, index)
    assert lazy.objects[2].properties[0].value == WaifReference(0)
    assert 0 in lazy.waifs
    assert lazy.waifs[0] == full.waifs[0]


@pytest.mark.parametrize("path", V17_DUMPS, ids=lambda p: p.name)
def test_objects_with_programs_carry_every_verb_and_its_code(path):
    full = load(str(path))
    index, _ = build_index(path)
    lazy = LazyDatabase(path, index)
    expected = [o for o in sorted(full.objects.values(), key=lambda o: o.id) if any(v.code is not None for v in o.verbs)]
    found = lazy.objects_with_programs()
    assert [(o.id, o.name, o.verbs) for o in found] == [(o.id, o.name, o.verbs) for o in expected]
    # Read without their properties, so they must not stand in for the real objects.
    assert not lazy.objects.loaded
    assert lazy.objects[expected[0].id] == expected[0]


def test_asking_for_every_object_parses_once_and_keeps_loaded_objects(monkeypatch):
    import lambdamoo_db.lazy as lazy_module

    full = load(str(TOASTCORE))
    index, _ = build_index(TOASTCORE)
    lazy = LazyDatabase(TOASTCORE, index)
    already = lazy.objects[20]
    parses = []
    monkeypatch.setattr(lazy_module, "load", lambda path: parses.append(path) or full)
    assert list(lazy.objects.values()) == list(full.objects.values())
    assert dict(lazy.objects.items()) == full.objects
    assert parses == [str(TOASTCORE)]
    assert lazy.objects[20] is already


def test_open_indexed_writes_reuses_and_prunes(tmp_path):
    dump_path = tmp_path / "world.db"
    dump_path.write_bytes(TOASTCORE.read_bytes())
    cache = tmp_path / "cache"
    first = open_indexed(dump_path, cache)
    assert [p.name for p in cache.iterdir()] == [index_path_for(dump_path, cache).name]
    second = open_indexed(dump_path, cache)
    assert isinstance(second, LazyDatabase)
    assert second.objects[20] == first.objects[20]

    # A new version of the same dump replaces the old index instead of piling up.
    stat = dump_path.stat()
    os.utime(dump_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    open_indexed(dump_path, cache)
    assert list(cache.iterdir()) == [index_path_for(dump_path, cache)]


def test_open_indexed_parses_older_formats_whole(tmp_path):
    db = open_indexed(PROJECT_ROOT / "LambdaCore-latest.db", tmp_path / "cache")
    assert not isinstance(db, LazyDatabase)
    assert db.objects[1].name == "Root Class"
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize(
    "query",
    [
        ["code", "$string_utils:from_list"],
        ["obj", "$string_utils"],
        ["props", "$string_utils"],
        ["grep", "-F", "-C", "1", "tostr("],
        ["grep", "-l", "notify", "-o", "$string_utils"],
        ["find", "--verb", "from_list"],
        ["refs", "$string_utils"],
        ["values", "string"],
        ["children", "-r", "1"],
        ["players"],
        ["tasks", "-v"],
        ["info"],
    ],
    ids=" ".join,
)
def test_cli_answers_from_the_index_as_from_a_full_parse(query, tmp_path):
    plain = CliRunner().invoke(moodb, ["--db", str(TOASTCORE), "--no-cache", *query])
    assert plain.exit_code == 0, plain.output
    for _ in range(2):  # the first run builds the index, the second reads through it
        r = CliRunner().invoke(moodb, ["--db", str(TOASTCORE), "--cache-dir", str(tmp_path), *query])
        assert r.exit_code == 0, r.output
        assert r.output == plain.output


def test_cli_batch_runs_mixed_queries_on_one_load(tmp_path):
    args = ["--db", str(TOASTCORE), "--cache-dir", str(tmp_path)]
    queries = ["prop $string_utils.name #2.wizard", "code -n '$string_utils:from_list'", "children $generic_utils"]
    expected = ""
    for q in queries:
        expected += f"=== {q}\n" + CliRunner().invoke(moodb, [*args, *shlex.split(q)]).output
    r = CliRunner().invoke(moodb, [*args, "batch", *queries])
    assert r.exit_code == 0, r.output
    assert r.output == expected

    r = CliRunner().invoke(moodb, [*args, "batch"], input="\n".join(queries) + "\n")
    assert r.exit_code == 0, r.output
    assert r.output == expected


def test_cli_batch_stops_at_the_first_failing_query(tmp_path):
    args = ["--db", str(TOASTCORE), "--cache-dir", str(tmp_path)]
    r = CliRunner().invoke(moodb, [*args, "batch", "prop #2.wizard", "obj #999999", "prop #2.name"])
    assert r.exit_code == 1
    assert "#2.wizard = 1" in r.output and "does not exist" in r.output
    assert "#2.name" not in r.output.split("does not exist")[1]


def test_cli_batch_accepts_empty_stdin_and_blank_lines():
    for text in ("", "\n \n\t\n"):
        r = CliRunner().invoke(moodb, ["batch"], input=text)
        assert r.exit_code == 0, r.output
        assert r.output == ""


def test_added_reads_real_dumps_with_and_without_the_index(tmp_path):
    old_path = tmp_path / "old.db"
    new_path = tmp_path / "new.db"
    old_path.write_bytes(TOASTCORE.read_bytes())
    full = load(str(old_path))
    obj = next(o for o in full.objects.values() if any(v.code for v in o.verbs))
    index, verb = next((i, v) for i, v in enumerate(obj.verbs) if v.code)
    verb.code.append('  notify("added regression");')
    with new_path.open("w", encoding="latin-1", newline="") as output:
        dump(full, output)
    args = ["--db", str(new_path)]
    query = ["added", str(old_path), "added regression"]
    plain = CliRunner().invoke(moodb, [*args, "--no-cache", *query])
    assert plain.exit_code == 0, plain.output
    assert f"#{obj.id}:[{index}]" in plain.output
    assert '  notify("added regression");' in plain.output
    assert plain.output.endswith("1 added lines in 1 verbs\n")
    indented = CliRunner().invoke(moodb, [*args, "--no-cache", "added", str(old_path), r"^  notify\("])
    assert indented.exit_code == 0, indented.output
    assert indented.output == plain.output
    for _ in range(2):
        indexed = CliRunner().invoke(moodb, [*args, "--cache-dir", str(tmp_path / "cache"), *query])
        assert indexed.exit_code == 0, indexed.output
        assert indexed.output == plain.output

    from lambdamoo_db.inspection import added_verb_lines

    old = open_indexed(old_path, tmp_path / "cache")
    new = open_indexed(new_path, tmp_path / "cache")
    assert len(list(added_verb_lines(old, new))) == 1
    assert not old.objects.loaded and not new.objects.loaded

    identical = CliRunner().invoke(moodb, [*args, "--cache-dir", str(tmp_path / "cache"), "added", str(new_path)])
    assert identical.exit_code == 0, identical.output
    assert identical.output == "0 added lines in 0 verbs\n"
