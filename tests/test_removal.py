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


def test_removal_cli_reports_players_and_verb_owners():
    result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(TOASTCORE), "removal", "#2"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "remove 1 objects: 1 chosen, 0 descendants"
    assert lines[1] == 'players removed: #2 "Wizard"'
    assert any(line.split() == ["1010", "verb.owner"] for line in lines)
