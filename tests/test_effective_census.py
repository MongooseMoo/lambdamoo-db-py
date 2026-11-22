"""`props -r --effective`: inherited values for a whole family in one walk."""

from io import StringIO

import pytest
from click.testing import CliRunner

import lambdamoo_db.cli as cli
from lambdamoo_db.database import CLEAR, MooDatabase, MooObject, ObjNum, Property
from lambdamoo_db.inspection import dollar_names, located_in, lookup_property
from lambdamoo_db.reader import Reader

FORM = [ObjNum(50), "u"]


def family() -> MooDatabase:
    """$thing #1 defines .form = {} and .dimensions; $chair #3 sets a form, the stool #5 blanks it again.

    #6 is an unregistered generic under $thing. $room #10 and $spatial #11 are rooms under $thing.
    Locations: the box #2 is in the spatial room #12; the lamp #7 is in the box; the chair #4 is in
    the plain room #13; everything else is nowhere.
    """
    db = MooDatabase()

    def add(num, name, parents, location=-1, own=(), values=()):
        o = MooObject(num, name, 0, 0, location, [ObjNum(p) for p in parents])
        names = list(own) + [None] * (len(values) - len(own))
        o.properties = [Property(n, v, 0, 5) for n, v in zip(names, values)]
        o.propdefs_count = len(own)
        db.objects[num] = o
        for p in parents:
            db.objects[p].children.append(ObjNum(num))

    add(0, "system", [], own=["thing", "chair", "room", "spatial"],
        values=[ObjNum(1), ObjNum(3), ObjNum(10), ObjNum(11)])
    add(1, "generic thing", [], own=["form", "dimensions"], values=[[], [1.0, 1.0, 1.0]])
    add(2, "plain box", [1], location=12, values=[CLEAR, CLEAR])
    add(3, "generic chair", [1], values=[FORM, [0.5, 0.5, 1.0]])
    add(4, "a chair", [3], location=13, values=[CLEAR, CLEAR])
    add(5, "a stool", [3], values=[[], CLEAR])
    add(6, "unregistered generic", [1], values=[CLEAR, CLEAR])
    add(7, "lamp", [6], location=2, values=[CLEAR, [0.2, 0.2, 0.4]])
    add(10, "generic room", [1], values=[CLEAR, CLEAR])
    add(11, "generic spatial room", [10], values=[CLEAR, CLEAR])
    add(12, "a spatial room", [11], values=[CLEAR, CLEAR])
    add(13, "a plain room", [10], values=[CLEAR, CLEAR])
    for o in db.objects.values():
        Reader(StringIO()).process_propnames(db, o)
    return db


@pytest.fixture
def props(monkeypatch):
    db = family()
    monkeypatch.setattr(cli, "_db", lambda ctx: (db, dollar_names(db)))

    def run(*args):
        result = CliRunner().invoke(cli.moodb, ["props", "#1", "-r", *args])
        assert result.exit_code == 0, result.output
        return result.output.splitlines()

    return run


def test_memo_gives_the_same_values_as_single_lookups():
    db = family()
    memo: dict = {}
    for num in (7, 5, 4, 2, 13, 12, 6, 3, 1, 11, 10):  # deepest first, so later lookups hit the memo
        for name in ("form", "dimensions"):
            alone, shared = lookup_property(db, db.objects[num], name), lookup_property(db, db.objects[num], name, memo)
            assert (shared.value, shared.value_from.id) == (alone.value, alone.value_from.id), (num, name)
    assert memo[(12, "form")] == ([], db.objects[1])


def test_located_in_follows_holders_however_deep():
    db = family()
    spatial = db.objects[11]
    places: dict = {}
    assert [n for n in (1, 2, 3, 4, 5, 6, 7, 12, 13) if located_in(db, db.objects[n], spatial, places)] == [2, 7]
    assert places[2] is True and places[12] is True and places[13] is False


def test_effective_values_group_counts_inherited_holders(props):
    # Stored values only: #1, #3 and #5 hold a form. Effective: #4 inherits #3's, and the rest #1's {}.
    assert props("--name", "form", "--group") == [
        ".form = {}  x2  #1 #5",
        '.form = {#50, "u"}  x1  #3',
    ]
    assert props("--name", "form", "--group", "--effective", "--skip-under", "$room") == [
        ".form = {}  x5  #1 #2 #6 #5 #7",  # descendants() walk order
        '.form = {#50, "u"}  x2  #3 #4',
    ]


def test_by_ancestor_counts_objects_without_a_form_by_registered_family(props):
    lines = props("--name", "form", "--effective", "--skip-under", "$room", "--truth", "false", "--by-ancestor",
                  "--show", "dimensions", "--examples", "2")
    assert lines == [
        "5 objects in 2 groups by nearest registered ancestor",
        '      4  #1 $thing "generic thing"  .dimensions mostly {1.0, 1.0, 1.0} x3'
        '  e.g. #1 "generic thing" {1.0, 1.0, 1.0}, #2 "plain box" {1.0, 1.0, 1.0}',
        '      1  #3 $chair "generic chair"  .dimensions mostly {0.5, 0.5, 1.0} x1  e.g. #5 "a stool" {0.5, 0.5, 1.0}',
    ]
    assert props("--name", "form", "--effective", "--skip-under", "$room", "--truth", "true", "--by-ancestor")[0] == \
        "2 objects in 1 groups by nearest registered ancestor"
    # Without the skip, the four rooms are counted under their own families.
    assert props("--name", "form", "--effective", "--truth", "false", "--by-ancestor")[:3] == [
        "9 objects in 4 groups by nearest registered ancestor",
        '      4  #1 $thing "generic thing"  e.g. #1 "generic thing", #2 "plain box", #6 "unregistered generic"',
        '      2  #10 $room "generic room"  e.g. #10 "generic room", #13 "a plain room"',  # ties by number
    ]


def test_located_in_keeps_things_held_in_a_spatial_room(props):
    assert props("--name", "form", "--effective", "--skip-under", "$room", "--located-in", "$spatial",
                 "--truth", "false", "--by-ancestor") == [
        "2 objects in 1 groups by nearest registered ancestor",
        '      2  #1 $thing "generic thing"  e.g. #2 "plain box", #7 "lamp"',
    ]


def test_filters_need_effective(monkeypatch):
    db = family()
    monkeypatch.setattr(cli, "_db", lambda ctx: (db, dollar_names(db)))
    for args in (["-r", "--by-ancestor"], ["-r", "--truth", "false"], ["--effective"],
                 ["-r", "--effective", "--show", "dimensions"]):
        assert CliRunner().invoke(cli.moodb, ["props", "#1", *args]).exit_code == 2, args
