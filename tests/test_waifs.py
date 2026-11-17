import re

import pytest

from lambdamoo_db.database import CLEAR, MooDatabase, MooObject, ObjNum, Property, Waif, WaifReference
from lambdamoo_db.inspection import count_waifs, moo_truth, parse_waif_slot_spec


def exit_db() -> MooDatabase:
    """#1 an exit class (:aliases, :placed), #2 its child (:door), #3 an unrelated class, #4 a room holding them."""
    db = MooDatabase()

    def add(num, parents, own, values):
        o = MooObject(num, f"obj{num}", 0, 0, -1, [ObjNum(p) for p in parents])
        names = own + [None] * (len(values) - len(own))
        o.properties = [Property(n, v, 0, 5) for n, v in zip(names, values)]
        o.propdefs_count = len(own)
        db.objects[num] = o

    add(1, [], [":aliases", ":placed"], [[], []])
    add(2, [1], [":door"], ["", CLEAR, CLEAR])
    db.objects[1].children = [ObjNum(2)]
    add(3, [], [":aliases"], [["n"]])
    add(4, [], ["exits"], [[WaifReference(i) for i in range(6)]])
    # Slot order is the class's own ':' props, then its parent's: #2 is door, aliases, placed.
    db.waifs[0] = Waif(1, 2, [(0, ["north", "n"])], 2)
    db.waifs[1] = Waif(1, 2, [(0, ["gate", "north"]), (1, [1.0, 2.0, 0.0])], 2)
    db.waifs[2] = Waif(2, 2, [(0, "oak"), (1, ["South", "s"])], 3)
    db.waifs[3] = Waif(1, 2, [], 2)  # no aliases set: the class's {}
    db.waifs[4] = Waif(3, 2, [], 1)  # not an exit
    db.waifs[5] = Waif(1, 2, [(0, ["gate"])], 2)
    return db


COMPASS = re.compile(r"^(north|south|east|west|n|s|e|w)$", re.IGNORECASE)


def test_counts_each_waif_of_the_class_and_its_descendants():
    found = count_waifs(exit_db(), exit_db().objects[1])
    assert found.by_class == {1: 4, 2: 1}
    assert found.matched == 5


def test_groups_by_list_element_and_truth_with_class_defaults():
    db = exit_db()
    found = count_waifs(db, db.objects[1], [parse_waif_slot_spec("aliases[1]"), parse_waif_slot_spec("placed?")])
    assert found.groups == {
        ('"north"', "false"): 1,
        ('"gate"', "true"): 1,
        ('"gate"', "false"): 1,
        ('"South"', "false"): 1,
        ("none", "false"): 1,
    }


def test_where_matches_any_string_in_a_list_and_negates():
    db = exit_db()
    first_not_compass = (parse_waif_slot_spec("aliases[1]"), COMPASS, True)
    some_compass = (parse_waif_slot_spec("aliases"), COMPASS, False)
    found = count_waifs(db, db.objects[1], [parse_waif_slot_spec("aliases")], [first_not_compass, some_compass])
    assert found.matched == 1
    assert found.groups == {('{"gate", "north"}',): 1}


def test_a_slot_the_class_lacks_is_none():
    db = exit_db()
    found = count_waifs(db, db.objects[1], [parse_waif_slot_spec("door")])
    assert found.groups == {("none",): 4, ('"oak"',): 1}


def test_spec_parsing_and_truth():
    s = parse_waif_slot_spec(":aliases[2]?")
    assert (s.name, s.index, s.truth) == ("aliases", 2, True)
    s = parse_waif_slot_spec("placed?")
    assert (s.name, s.index, s.truth) == ("placed", None, True)
    for bad in ("aliases[0]", "a~b", ""):
        with pytest.raises(ValueError):
            parse_waif_slot_spec(bad)
    assert [moo_truth(v) for v in (0, 1, 0.0, "", "x", [], [0], {}, ObjNum(5))] == [False, True, False, False, True, False, True, False, False]
