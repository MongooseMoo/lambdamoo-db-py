import re
from pathlib import Path

from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import CLEAR, MooDatabase, MooObject, ObjNum, Property, Waif, WaifReference
from lambdamoo_db.inspection import find_strings, waif_slot_names

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"


def waif_db() -> MooDatabase:
    """#1 base waif class, #2 sound class (child of #1), #3 system class, #4 a ship, #5 its child."""
    db = MooDatabase()

    def add(num, parents, own, values, anon=False):
        o = MooObject(num, f"obj{num}", 0, 0, -1, [ObjNum(p) for p in parents], anon=anon)
        names = own + [None] * (len(values) - len(own))
        o.properties = [Property(n, v, 0, 5) for n, v in zip(names, values)]
        o.propdefs_count = len(own)
        db.objects[num] = o

    add(1, [], [":handle", "plain"], ["", "base plain"])
    add(2, [1], [":snd", ":volume"], ["", 0, CLEAR, CLEAR])
    add(3, [], [":sounds", ":parent"], [[], 0])
    add(4, [], ["systems", "note"], [[WaifReference(0), "spare part"], {"door": "doors/creak.m4a", "Old Key.ogg": 1}])
    add(5, [4], [], [CLEAR, CLEAR])
    add(6, [], ["held"], [WaifReference(1)], anon=True)
    # waif 0: a system whose sounds map holds waif 1 and which points back at itself.
    db.waifs[0] = Waif(3, 2, [(0, {"engage": WaifReference(1)}), (1, WaifReference(0))], 2)
    # waif 1: a sound. Slot order is the class's own ':' props, then its parent's.
    db.waifs[1] = Waif(2, 2, [(0, "ship/Warp Loop.ogg"), (2, "s42")], 3)
    return db


def hits(db, pattern):
    return [(h.obj.id, h.where, h.value, h.waif_classes) for h in find_strings(db, re.compile(pattern))]


def test_waif_slot_names_follow_the_class_then_its_ancestors():
    db = waif_db()
    assert waif_slot_names(db, 2) == ["snd", "volume", "handle"]
    assert waif_slot_names(db, 3) == ["sounds", "parent"]
    assert waif_slot_names(db, 999) == []


def test_find_strings_follows_waifs_inside_waifs_and_names_the_path():
    db = waif_db()
    assert hits(db, r"\.ogg$") == [
        (4, '.systems[1]<waif #3>.sounds["engage"]<waif #2>.snd', "ship/Warp Loop.ogg", (3, 2)),
        (4, '.note.keys["Old Key.ogg"]', "Old Key.ogg", ()),
        (6, ".held<waif #2>.snd", "ship/Warp Loop.ogg", (2,)),
    ]
    assert hits(db, "^s42$") == [
        (4, '.systems[1]<waif #3>.sounds["engage"]<waif #2>.handle', "s42", (3, 2)),
        (6, ".held<waif #2>.handle", "s42", (2,)),
    ]


def test_find_strings_reads_lists_and_map_values_and_skips_clear_slots():
    db = waif_db()
    assert hits(db, "^spare part$") == [(4, ".systems[2]", "spare part", ())]
    assert hits(db, "creak") == [(4, '.note["door"]', "doors/creak.m4a", ())]
    # #5 inherits both slots clear, so #4's values are not repeated for it.
    assert {h[0] for h in hits(db, ".")} == {1, 4, 6}
    assert hits(db, "^base plain$") == [(1, ".plain", "base plain", ())]


def run(*args):
    return CliRunner().invoke(moodb, ["--db", str(TOASTCORE), "--no-cache", *args])


def test_cli_values():
    r = run("values", "^Welcome to the Toast")
    assert r.exit_code == 0, r.output
    assert r.output == '#10 $login "Login Commands".welcome_message[1] = "Welcome to the ToastCore database."\n'

    assert run("values", "-F", "-i", "WELCOME TO THE TOASTCORE").output == r.output
    assert run("values", "^Welcome to the Toast", "--in-waif", "#1").output == ""

    r = run("values", "(")
    assert r.exit_code == 2 and "Invalid value for PATTERN" in r.output
    r = run("values", "x", "--in-waif", "#999999")
    assert r.exit_code == 1 and "does not exist" in r.output
