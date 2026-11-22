"""`unlit`: sort rooms by where their light could come from."""

from io import StringIO

import pytest
from click.testing import CliRunner

import lambdamoo_db.cli as cli
from lambdamoo_db.database import CLEAR, MooDatabase, MooObject, ObjNum, Property, Verb
from lambdamoo_db.inspection import dollar_names
from lambdamoo_db.reader import Reader


def world() -> MooDatabase:
    """$room #10 defines .outdoor = 0 and .ambient_light = 0.0; $lamp #3 has :light_emission.

    The meadow #11 is outdoor and the lit hall #12 has a fill. The study #13 holds a box #20 holding a
    lamp #21. The cellar #14 holds a rock #22 with :glow. The generic cave #15 and its grotto #16
    (ambient_light -0.2) hold nothing.
    """
    db = MooDatabase()

    def add(num, name, parents, location=-1, own=(), values=(), verbs=()):
        o = MooObject(num, name, 0, 0, location, [ObjNum(p) for p in parents])
        names = list(own) + [None] * (len(values) - len(own))
        o.properties = [Property(n, v, 0, 5) for n, v in zip(names, values)]
        o.propdefs_count = len(own)
        o.verbs = [Verb(v, 0, 0, -1, num) for v in verbs]
        db.objects[num] = o
        for p in parents:
            db.objects[p].children.append(ObjNum(num))
        if location in db.objects:
            db.objects[location].contents.append(ObjNum(num))

    add(0, "system", [], own=["thing", "lamp", "room"], values=[ObjNum(1), ObjNum(3), ObjNum(10)])
    add(1, "generic thing", [])
    add(3, "generic lamp", [1], verbs=["light_emission"])
    add(10, "generic room", [], own=["outdoor", "ambient_light"], values=[0, 0.0])
    add(11, "meadow", [10], values=[1, 0.5])
    add(12, "lit hall", [10], values=[CLEAR, 0.5])
    add(13, "study", [10], values=[CLEAR, CLEAR])
    add(14, "cellar", [10], values=[CLEAR, CLEAR])
    add(15, "generic cave", [10], values=[0, CLEAR])
    add(16, "grotto", [15], values=[CLEAR, -0.2])
    add(20, "box", [1], location=13)
    add(21, "lamp", [3], location=20)
    add(22, "rock", [1], location=14, verbs=["glow"])
    for o in db.objects.values():
        Reader(StringIO()).process_propnames(db, o)
    return db


@pytest.fixture
def unlit(monkeypatch):
    db = world()
    monkeypatch.setattr(cli, "_db", lambda ctx: (db, dollar_names(db)))

    def run(*args, code=0):
        result = CliRunner().invoke(cli.moodb, ["unlit", *args])
        assert result.exit_code == code, result.output
        return result.output.splitlines()

    return run


def test_rooms_are_sorted_by_sky_fill_source_or_none(unlit):
    assert unlit() == [
        '6 rooms under #10 $room "generic room"; a thing with :light_emission is a light source',
        ' outdoor       1  e.g. #11 "meadow"',  # outdoor wins over its fill
        '  filled       1  e.g. #12 "lit hall"',
        ' sourced       1  e.g. #13 "study"',  # the lamp is inside a box
        '    dark       3  e.g. #14 "cellar", #15 "generic cave", #16 "grotto"',  # a negative fill is none
    ]


def test_list_prints_every_room_of_a_kind_and_limit_zero_drops_examples(unlit):
    assert unlit("--list", "dark", "--limit", "0") == [
        '6 rooms under #10 $room "generic room"; a thing with :light_emission is a light source',
        ' outdoor       1',
        '  filled       1',
        ' sourced       1',
        '    dark       3',
        '#14 "cellar"',
        '#15 "generic cave"',
        '#16 "grotto"',
    ]


def test_verb_names_what_counts_as_a_light_source(unlit):
    lines = unlit("--verb", "glow", "--limit", "1")
    assert lines[0].endswith("a thing with :glow is a light source")
    assert lines[3:] == [' sourced       1  e.g. #14 "cellar"', '    dark       3  e.g. #13 "study"']


def test_a_root_without_the_properties_is_an_error(unlit):
    assert unlit("$thing", code=1)[-1] == "Error: property 'outdoor' not found on #1"
