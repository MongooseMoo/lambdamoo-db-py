from pathlib import Path

from click.testing import CliRunner

import lambdamoo_db.cli as cli
from lambdamoo_db.database import ObjNum, Property
from lambdamoo_db.reader import load
from lambdamoo_db.removal import plan

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"


def descendants(db, oid):
    out, todo = set(), [oid]
    while todo:
        for child in db.objects[todo.pop()].children:
            out.add(int(child))
            todo.append(int(child))
    return out


def test_the_set_takes_every_descendant_and_counts_only_outside_holders():
    db = load(str(TOASTCORE))
    parent = max(db.objects, key=lambda oid: len(descendants(db, oid)) if oid > 1 else 0)
    p = plan(db, [parent])
    assert p.descendants == descendants(db, parent)
    assert p.removed == {parent} | p.descendants
    for (_, holder, target), _n in p.inbound.items():
        assert target in p.removed
        assert not (holder.startswith("#") and int(holder[1:]) in p.removed)


def test_set_lists_and_structural_fields_are_dropped_not_inbound():
    db = load(str(TOASTCORE))
    victim = next(oid for oid, o in db.objects.items() if not o.children and int(o.location) >= 0 and oid > 1)
    holder = next(oid for oid in db.objects if oid not in (victim, int(db.objects[victim].location)))
    db.objects[holder].properties.append(Property("owned_objects",[ObjNum(victim)], holder, 0))
    p = plan(db, [victim])
    assert p.dropped[".owned_objects"] == 1
    assert p.dropped["contents"] >= 1  # its location's contents list
    assert not any(field in (".owned_objects", "contents", "children") for field, _, _ in p.inbound)


def test_a_reference_from_outside_is_reported_with_its_field_and_holder():
    db = load(str(TOASTCORE))
    victim = next(oid for oid, o in db.objects.items() if not o.children and oid > 1 and oid not in db.players)
    holder = next(oid for oid in db.objects if oid != victim)
    db.objects[holder].properties.append(Property("favourite",{"x": ObjNum(victim)}, holder, 0))
    p = plan(db, [victim])
    assert p.inbound[(".favourite", f"#{holder}", victim)] == 1


def test_with_contents_takes_what_is_inside_but_never_a_player_or_an_older_object(monkeypatch):
    import lambdamoo_db.removal as removal

    db = load(str(TOASTCORE))
    players = {int(p) for p in db.players}
    room = next(oid for oid, o in db.objects.items()
                if oid > 1 and not o.children and sum(int(c) not in players for c in o.contents) >= 2)
    things = sorted(int(c) for c in db.objects[room].contents if int(c) not in players)
    older, things = things[0], set(things[1:])
    visitor = next(p for p in sorted(players) if int(db.objects[p].location) != room)
    if int(db.objects[visitor].location) >= 0:
        db.objects[int(db.objects[visitor].location)].contents.remove(ObjNum(visitor))
    db.objects[visitor].location = ObjNum(room)
    db.objects[room].contents.append(ObjNum(visitor))
    monkeypatch.setattr(removal, "_created", lambda db, oid: 50 if oid == older else 100)
    assert plan(db, [room]).stranded.keys() >= things | {older, visitor}
    p = plan(db, [room], with_contents=True)
    assert things <= p.contents <= p.removed
    assert visitor not in p.removed and p.stranded[visitor] == room
    assert older not in p.removed and p.stranded[older] == room
    monkeypatch.setattr(removal, "_created", lambda db, oid: None)  # no times: nothing is taken
    assert not plan(db, [room], with_contents=True).contents


def test_created_falls_back_to_first_placement_only_when_creation_time_is_unset():
    from lambdamoo_db.removal import _created

    db = load(str(TOASTCORE))
    oid = next(oid for oid, o in db.objects.items() if oid > 1 and o.propdefs_count)
    o = db.objects[oid]
    o.properties[0].propertyName = "creation_time"
    o.properties[0].value = 700
    assert _created(db, oid) == 700
    o.properties[0].value = 0
    o.last_move = {"source": ObjNum(-1), "time": 500}
    assert _created(db, oid) == 500
    o.last_move = {"source": ObjNum(2), "time": 500}  # moved in from somewhere: no evidence
    assert _created(db, oid) is None
    o.last_move = -1
    assert _created(db, oid) is None
    from lambdamoo_db.inspection import CLEAR, find_slot

    child = next(c for c in db.objects.values() if o.id in [int(p) for p in c.parents])
    child.properties[find_slot(db, child, "creation_time").index].value = CLEAR  # inherits the parent's value
    child.last_move = {"source": ObjNum(-1), "time": 900}
    o.properties[0].value = 700
    assert _created(db, child.id) == 900


def test_refs_skip_leaves_out_named_properties(tmp_path):
    from io import StringIO

    from lambdamoo_db.writer import Writer

    db = load(str(TOASTCORE))
    victim = next(oid for oid, o in db.objects.items() if oid > 1)
    slots = db.objects[0].properties[:2]
    slots[0].value = [ObjNum(victim)]
    slots[1].value = {"k": ObjNum(victim)}
    out = StringIO()
    Writer(db=db, output_file=out).writeDatabase()
    path = tmp_path / "refs.db"
    path.write_bytes(out.getvalue().encode("latin-1"))
    first, second = slots[0].propertyName, slots[1].propertyName

    def where(*extra):
        result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(path), "refs", f"#{victim}", *extra])
        assert result.exit_code == 0, result.output
        return [line for line in result.output.splitlines() if line.startswith("#0")]

    assert any(f".{first}[" in line for line in where()) and any(f".{second}" in line for line in where())
    kept = where("--skip", first.upper())
    assert not any(f".{first}[" in line for line in kept) and any(f".{second}" in line for line in kept)


def test_removal_cli_chooses_by_name_and_lists_them():
    db = load(str(TOASTCORE))
    wizards = sorted(oid for oid, o in db.objects.items() if o.name.lower() == "wizard")
    result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(TOASTCORE), "removal", "--name", "^wizard$"])
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if line.startswith("--name"))
    assert line.startswith(f"--name ^wizard$: {len(wizards)}: #{wizards[0]} ")


def test_removal_cli_reports_players_and_verb_owners():
    result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(TOASTCORE), "removal", "#2"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "remove 1 objects: 1 chosen, 0 descendants"
    assert lines[1] == 'players removed: #2 "Wizard"'
    assert any(line.split() == ["1010", "verb.owner"] for line in lines)


def test_removal_cli_field_lists_only_that_fields_slots():
    def listed(*extra):
        result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(TOASTCORE), "removal", "#2", *extra])
        assert result.exit_code == 0, result.output
        return [line.split()[0] for line in result.output.splitlines() if " -> " in line]

    assert {"verb.owner", "owner"} <= set(listed("--limit", "0"))  # 0 = all
    assert set(listed("--field", "owner")) == {"owner"}
