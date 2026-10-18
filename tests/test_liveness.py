from pathlib import Path

from click.testing import CliRunner

import lambdamoo_db.cli as cli
from lambdamoo_db.database import MooObject, ObjNum, Property
from lambdamoo_db.liveness import unreachable
from lambdamoo_db.reader import load
from lambdamoo_db.removal import plan

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"


def _loose(db):
    """A new thing, a child of the generic thing, nowhere, that nothing names."""
    thing = int(next(p.value for p in db.objects[0].properties if p.propertyName == "thing"))
    oid = max(db.objects) + 1
    db.objects[oid] = MooObject(oid, "loose", 0, ObjNum(2), ObjNum(-1), [ObjNum(thing)])
    assert not plan(db, [oid]).inbound
    return oid


def test_roots_and_what_they_hold_are_reached():
    db = load(str(TOASTCORE))
    gone = unreachable(db)
    assert 0 not in gone and not gone & {int(p) for p in db.players}
    for name in ("room", "thing", "player"):  # $names are #0's properties
        assert int(next(p.value for p in db.objects[0].properties if p.propertyName == name)) not in gone


def test_an_object_nothing_names_is_unreachable_until_something_does():
    db = load(str(TOASTCORE))
    loose = _loose(db)
    assert loose in unreachable(db)
    db.objects[0].properties.append(Property("keepsake", {"k": [ObjNum(loose)]}, 0, 0))
    assert loose not in unreachable(db)
    assert loose in unreachable(db, skip={".keepsake"})  # a skipped property keeps nothing alive


def test_what_an_unreachable_object_holds_goes_with_it_and_ownership_keeps_nothing():
    db = load(str(TOASTCORE))
    loose = _loose(db)
    inside = _loose(db)
    db.objects[inside].location = ObjNum(loose)
    db.objects[loose].contents.append(ObjNum(inside))
    wizard = int(db.players[0])
    db.objects[wizard].properties.append(Property("owned_objects", [ObjNum(loose), ObjNum(inside)], wizard, 0))
    db.objects[loose].owner = ObjNum(wizard)
    assert {loose, inside} <= unreachable(db)


def test_an_object_held_only_by_a_removed_one_becomes_unreachable():
    db = load(str(TOASTCORE))
    holder, held = _loose(db), _loose(db)
    db.objects[0].properties.append(Property("keepsake", [ObjNum(holder)], 0, 0))
    db.objects[holder].properties.append(Property("friend", [ObjNum(held)], holder, 0))
    assert not {holder, held} & unreachable(db)
    assert unreachable(db, gone={holder}) == {held}  # the removed holder itself is not listed


def test_a_number_spelled_in_a_string_is_reported_as_a_mention():
    from lambdamoo_db.liveness import mentions

    db = load(str(TOASTCORE))
    loose = _loose(db)
    assert loose not in mentions(db, {loose})
    db.objects[0].properties.append(Property("note", ["see", {"x": f"room #{loose} upstairs"}], 0, 0))
    assert mentions(db, {loose})[loose] == {"#0.note"}
    assert loose in unreachable(db)  # a string does not keep it alive; it is only reported


def test_mentions_cli_counts_objects_spelled_in_each_place(tmp_path):
    from io import StringIO

    from lambdamoo_db.writer import Writer

    db = load(str(TOASTCORE))
    db.objects[0].properties[0].value = {"#2:fall": 1, "#1:x": 2, "#999999": 3}  # the last names nothing
    out = StringIO()
    Writer(db=db, output_file=out).writeDatabase()
    path = tmp_path / "keys.db"
    path.write_bytes(out.getvalue().encode("latin-1"))
    result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(path), "mentions", "--limit", "0"])
    assert result.exit_code == 0, result.output
    place = f"#0.{db.objects[0].properties[0].propertyName}"
    assert any(line.split() == ["2", place] for line in result.output.splitlines())


def test_unreachable_cli_groups_by_outermost_object_with_owners_and_mentions(tmp_path):
    from io import StringIO

    from lambdamoo_db.writer import Writer

    db = load(str(TOASTCORE))
    loose = _loose(db)
    inside = _loose(db)
    db.objects[inside].location = ObjNum(loose)
    db.objects[loose].contents.append(ObjNum(inside))
    db.objects[0].properties[0].value = f"see #{loose}"
    db.total_objects = max(db.objects) + 1
    out = StringIO()
    Writer(db=db, output_file=out).writeDatabase()
    path = tmp_path / "loose.db"
    path.write_bytes(out.getvalue().encode("latin-1"))
    result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(path), "unreachable"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "unreachable: 2 objects in 1 groups"
    assert lines[1].split()[:2] == ["2", f"#{loose}"]
    assert any(line.split() == ["2", "#2", '"Wizard"', "[player]"] for line in lines)
    assert any(line.startswith(f"    #{loose} ") and "#0." in line for line in lines)
