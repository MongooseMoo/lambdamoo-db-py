from io import StringIO
from pathlib import Path

from click.testing import CliRunner

import lambdamoo_db.cli as cli
from lambdamoo_db.database import ObjNum
from lambdamoo_db.reader import load
from lambdamoo_db.writer import Writer

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"


def dump_with(tmp_path, value):
    """toastcore with #0's first property holding ``value``; returns the path and the property's name."""
    db = load(str(TOASTCORE))
    assert db.objects[0].propdefs_count  # properties[0] is #0's own definition
    slot = db.objects[0].properties[0]
    slot.value = value
    out = StringIO()
    Writer(db=db, output_file=out).writeDatabase()
    path = tmp_path / "keys.db"
    path.write_bytes(out.getvalue().encode("latin-1"))
    return path, slot.propertyName, db


def run(tmp_path, path, spec):
    result = CliRunner().invoke(cli.moodb, ["--cache-dir", str(tmp_path / "cache"), "--db", str(path), "keys", spec])
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def test_keys_groups_map_keys_by_what_they_name(tmp_path):
    db = load(str(TOASTCORE))
    player = min(int(p) for p in db.players)
    thing = min(oid for oid in db.objects if oid > 1 and oid not in {int(p) for p in db.players})
    gone = min(db.recycled_objects)
    value = {ObjNum(player): 1, ObjNum(thing): 2, ObjNum(gone): 3, ObjNum(gone + 100000): 4, ObjNum(-1): 5, "x": 6}
    path, name, _ = dump_with(tmp_path, value)
    lines = run(tmp_path, path, f"#0.{name}")
    assert lines[0] == f"#0.{name}: map of 6 keys"
    kinds = {line.split()[0]: line for line in lines[1:]}
    assert set(kinds) == {"player", "object", "missing", "nothing", "STR"}
    assert kinds["missing"].split()[1] == "2"
    assert f"#{player}" in kinds["player"] and f"#{gone}" in kinds["missing"]


def test_keys_groups_list_elements(tmp_path):
    db = load(str(TOASTCORE))
    gone = min(db.recycled_objects)
    path, name, _ = dump_with(tmp_path, [ObjNum(1), ObjNum(gone), ObjNum(gone)])
    lines = run(tmp_path, path, f"#0.{name}")
    assert lines[0] == f"#0.{name}: list of 3 elements"
    assert [line.split()[:2] for line in lines[1:]] == [["missing", "2"], ["object", "1"]]
