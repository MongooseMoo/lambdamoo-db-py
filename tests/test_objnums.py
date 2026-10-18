from io import StringIO
from pathlib import Path

import attrs
import pytest

import lambdamoo_db.database as database
from lambdamoo_db.database import Anon, MooObject, ObjNum, Waif, WaifReference
from lambdamoo_db.objnums import FIELDS, remap
from lambdamoo_db.reader import load
from lambdamoo_db.writer import Writer

ROOT = Path(__file__).parent.parent
TOASTCORE = ROOT / "toastcore.db"
SUSPENDED = ROOT / "tests" / "fixtures" / "Suspended.db"
SHIFT = 1_000_000


def written(db) -> bytes:
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    return output.getvalue().encode("latin-1")


def shift(kind, number, where):
    return number + SHIFT if number >= 0 else number


def model_classes():
    return [c for c in vars(database).values()
            if attrs.has(c) and c.__module__ == database.__name__ and not issubclass(c, database._TypedScalar)]


def test_fields_declare_every_model_field():
    assert set(FIELDS) == set(model_classes())
    for cls, fields in FIELDS.items():
        assert set(fields) == {f.name for f in attrs.fields(cls)}, cls.__name__


@pytest.mark.parametrize("path", [TOASTCORE, ROOT / "toast2.db", SUSPENDED], ids=lambda p: p.name)
def test_unchanged_numbers_write_the_same_bytes(path):
    db = load(str(path))
    remap(db, lambda kind, number, where: number)
    assert written(db) == path.read_bytes()


def typed_numbers(value, found, seen):
    """Every ObjNum and Anon reachable through attrs fields, lists, tuples, sets and maps."""
    if isinstance(value, (ObjNum, Anon)):
        found.append(value)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            typed_numbers(item, found, seen)
    elif isinstance(value, dict):
        for key, item in value.items():
            typed_numbers(key, found, seen)
            typed_numbers(item, found, seen)
    elif attrs.has(type(value)) and id(value) not in seen:
        seen.add(id(value))
        for f in attrs.fields(type(value)):
            if FIELDS.get(type(value), {}).get(f.name) != "position":
                typed_numbers(getattr(value, f.name), found, seen)


def enriched_toastcore():
    """toastcore with a waif, an anonymous reference, object map keys, built-in data and a connection."""
    db = load(str(TOASTCORE))
    db.waifs = {0: Waif(waif_class=ObjNum(1), owner=ObjNum(2), props=[(0, {ObjNum(3): [ObjNum(4), Anon(5)]})])}
    db.objects[0].properties[0].value = [WaifReference(0), {ObjNum(6): ObjNum(7)}, Anon(-1)]
    db.connections = ["-5 0", "2 1"]
    activation = db.queuedTasks[0].activation
    activation.bi_func_data = ["bf_call_function data: fname = move", "bf_move data: what = 8, where = 9, position = 0"]
    return db


@pytest.mark.parametrize("make", [enriched_toastcore, lambda: load(str(SUSPENDED))], ids=["toastcore", "suspended"])
def test_no_typed_number_is_missed(make):
    db = make()
    before = []
    typed_numbers(db, before, set())
    assert any(int(n) >= 0 for n in before)

    remap(db, shift)

    after = []
    typed_numbers(db, after, set())
    assert len(after) == len(before)
    assert [n for n in after if 0 <= int(n) < SHIFT] == []


def test_raw_numbers_and_lines_are_shifted():
    db = enriched_toastcore()
    task = db.queuedTasks[0].activation
    this, programmer = int(task.this), int(task.programmer)
    owner = int(db.objects[5].owner)

    remap(db, shift)

    assert int(db.objects[5].owner) == owner + SHIFT
    assert int(task.this) == (this + SHIFT if this >= 0 else this)
    assert int(task.programmer) == programmer + SHIFT
    assert task.bi_func_data == ["bf_call_function data: fname = move",
                                 f"bf_move data: what = {8 + SHIFT}, where = {9 + SHIFT}, position = 0"]
    assert db.connections == ["-5 1000000", "1000002 1000001"]
    assert all(int(v.object) == o.id + SHIFT for o in db.objects.values() for v in o.verbs)


def test_code_literals_follow_the_lexer():
    db = load(str(TOASTCORE))
    verb = db.objects[0].verbs[0]
    verb.code = ['x = #12.name + "#5 \\"#6\\"";', "/* #7 */ y = {#-1, #0};", 'return "a\\\\" + tostr(#3);']
    seen = []

    remap(db, lambda kind, number, where: seen.append((kind, number, where)) or shift(kind, number, where))

    assert verb.code == [f'x = #{12 + SHIFT}.name + "#5 \\"#6\\"";', f"/* #7 */ y = {{#-1, #{SHIFT}}};",
                         f'return "a\\\\" + tostr(#{3 + SHIFT});']
    assert [(n, w[-1]) for k, n, w in seen if w[:3] == ("#0", ":0", "code")] == [(12, 1), (-1, 2), (0, 2), (3, 3)]


@pytest.mark.parametrize("line", ["x = #;", "x = #-;", "x = # 5;"])
def test_malformed_literal_raises(line):
    db = load(str(TOASTCORE))
    db.objects[0].verbs[0].code = [line]
    with pytest.raises(ValueError, match="malformed object literal"):
        remap(db, shift)


def test_unknown_built_in_data_raises():
    db = load(str(TOASTCORE))
    db.queuedTasks[0].activation.bi_func_data = ["bf_frobnicate data: 5"]
    with pytest.raises(ValueError, match="unrecognised"):
        remap(db, shift)


def test_merging_map_keys_raises():
    db = load(str(TOASTCORE))
    db.objects[0].properties[0].value = {ObjNum(1): 1, ObjNum(2): 2}
    with pytest.raises(ValueError, match="merged two map keys"):
        remap(db, lambda kind, number, where: 1 if number == 2 and kind == "object" else number)


def test_cli_roundtrip_through_remap_and_slots():
    from click.testing import CliRunner

    import lambdamoo_db.cli as cli

    runner = CliRunner()
    result = runner.invoke(cli.moodb, ["--no-cache", "--db", str(TOASTCORE), "roundtrip", "--remap"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("identical: ")

    result = runner.invoke(cli.moodb, ["--no-cache", "--db", str(TOASTCORE), "slots"])
    assert result.exit_code == 0, result.output
    rows = {tuple(line.split()[:2]): line.split()[2:] for line in result.output.splitlines()[1:]}
    assert rows[("object", "object.owner")] == ["127", "0", "0"]
    assert rows[("object", "queuedTasks.activation.programmer")] == ["1", "0", "0"]


def test_anonymous_holders_report_anon_kind():
    db = load(str(TOASTCORE))
    anon = MooObject(id=900, name="a", flags=0, owner=ObjNum(2), location=ObjNum(-1), anon=True)
    verb = database.Verb("v", ObjNum(2), 0, 0, 900)
    anon.verbs = [verb]
    db.objects[900] = anon
    kinds = []

    remap(db, lambda kind, number, where: kinds.append((kind, where)) or number)

    assert ("anon", ("anon 900", ":0", "object")) in kinds
