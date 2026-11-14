import json
from collections import Counter

from click.testing import CliRunner

import lambdamoo_db.cli as cli
from lambdamoo_db.database import CLEAR, MooDatabase, MooObject, ObjNum, Property, Verb, Waif, WaifReference
from lambdamoo_db.inspection import find_catalog_refs

SOCIAL = 3
CATALOG = 10


def sound_db() -> MooDatabase:
    """#0 names $social #3 and $catalog_sound #10; #2 holds every shape of sound value.

    #11 is a child of the catalog class, #12 an exit-like waif class with an
    ``:open_snd`` slot.
    """
    db = MooDatabase()

    def add(num, parents, own, values):
        o = MooObject(num, f"obj{num}", 0, 0, -1, [ObjNum(p) for p in parents])
        names = own + [None] * (len(values) - len(own))
        o.properties = [Property(n, v, 0, 5) for n, v in zip(names, values)]
        o.propdefs_count = len(own)
        db.objects[num] = o
        return o

    add(0, [], ["social", "catalog_sound"], [ObjNum(SOCIAL), ObjNum(CATALOG)])
    add(
        2,
        [],
        ["x_snd", "y_snd", "count", "loose", "tuned_snd", "path_note", "flag_snd", "exits", "cache"],
        [
            5,
            [{"group": 7}, "a.m4a#02"],
            3,
            {"sound": 13, "volume": 0},
            {"sound": 14, "volume": 1, "normalize": 1, "vary": [1, 2, 3, 4]},
            "doors/creak.ogg",
            True,
            [WaifReference(0)],
            [WaifReference(1), WaifReference(2)],
        ],
    )
    add(SOCIAL, [], ["greet"], [{"self": "You wave.", "sound": {"group": 21}, "sounds": {"person": 22}}])
    add(CATALOG, [], [":sound_id", ":content_id", ":snd"], [0, ObjNum(-1), ""])
    add(11, [CATALOG], [], [CLEAR, CLEAR, CLEAR])
    db.objects[CATALOG].children = [11]
    add(12, [], [":open_snd"], [0])
    db.waifs[0] = Waif(12, 2, [(0, 9)], 1)
    db.waifs[1] = Waif(11, 2, [(0, 11), (1, ObjNum(-1)), (2, "packs/b.m4a")], 3)
    db.waifs[2] = Waif(11, 2, [(0, 0), (1, 31)], 3)
    v = Verb("play", ObjNum(2), 13, -1, 1)
    v.code = ["n = 5 + #5 + 50;", 'x = "a.m4a#02";', '"5 a.m4a#02";']
    db.objects[2].verbs = [v]
    return db


EXPECTED = {
    (2, ".x_snd", "sound", 5, "snd-property"),
    (2, '.y_snd[1]["group"]', "group", 7, "snd-property"),
    (2, ".y_snd[2]", "slice", "a.m4a#02", "string"),
    (2, '.loose["sound"]', "sound", 13, "snd-map"),
    (2, '.tuned_snd["sound"]', "sound", 14, "snd-property"),
    (2, ".path_note", "path", "doors/creak.ogg", "string"),
    (2, ".exits[1]<waif #12>.open_snd", "sound", 9, "snd-slot"),
    (2, ".cache[1]<waif #11>.sound_id", "sound", 11, "catalog-waif"),
    (2, ".cache[1]<waif #11>.snd", "path", "packs/b.m4a", "string"),
    (2, ".cache[2]<waif #11>.content_id", "content", 31, "catalog-waif"),
    (SOCIAL, '.greet["sound"]["group"]', "group", 21, "social"),
    (SOCIAL, '.greet["sounds"]["person"]', "sound", 22, "social"),
}


def refs(db, **kwargs):
    return [(r.obj.id, r.where, r.kind, r.value, r.context) for r in find_catalog_refs(db, CATALOG, SOCIAL, **kwargs)]


def test_find_catalog_refs_follows_the_sound_value_grammar():
    found = refs(sound_db())
    assert len(found) == len(set(found))
    # Not reported: count = 3, the volume/normalize/vary numbers, flag_snd = true,
    # sound_id = 0 and the unset #-1 slots.
    assert set(found) == EXPECTED


def test_skipped_properties_are_not_entered_and_are_counted():
    skipped = Counter()
    found = refs(sound_db(), skip=["cach*"], skipped=skipped)
    assert set(found) == {r for r in EXPECTED if not r[1].startswith(".cache")}
    assert skipped == Counter({"cache": 1})


def run(monkeypatch, tmp_path, *args):
    db = sound_db()
    monkeypatch.setattr(cli, "_db", lambda _: (db, {}))
    return CliRunner().invoke(cli.moodb, ["sound-refs", *args])


def test_cli_lists_each_reference_with_where_it_is_held(monkeypatch, tmp_path):
    r = run(monkeypatch, tmp_path)
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert len(lines) == len(EXPECTED)
    assert '#2 "obj2".x_snd  sound  5  snd-property' in lines
    assert '#2 "obj2".y_snd[2]  slice  "a.m4a#02"  string' in lines

    r = run(monkeypatch, tmp_path, "--kind", "group", "--format", "jsonl")
    assert r.exit_code == 0, r.output
    assert [json.loads(line) for line in r.output.splitlines()] == [
        {"obj": 2, "where": '.y_snd[1]["group"]', "kind": "group", "value": 7, "context": "snd-property"},
        {"obj": SOCIAL, "where": '.greet["sound"]["group"]', "kind": "group", "value": 21, "context": "social"},
    ]


def test_cli_ids_file_selects_references_and_sets_the_exit_status(monkeypatch, tmp_path):
    ids = tmp_path / "ids.tsv"
    ids.write_text("sound\t5\nslice\ta.m4a#02\ngroup\t999\n")
    r = run(monkeypatch, tmp_path, "--ids", str(ids))
    assert r.exit_code == 1, r.output
    assert r.output.splitlines() == ['#2 "obj2".x_snd  sound  5  snd-property', '#2 "obj2".y_snd[2]  slice  "a.m4a#02"  string']

    ids.write_text("sound\t999\n")
    r = run(monkeypatch, tmp_path, "--ids", str(ids))
    assert (r.exit_code, r.output) == (0, "")

    ids.write_text("sound\tfive\n")
    assert run(monkeypatch, tmp_path, "--ids", str(ids)).exit_code == 2


def test_cli_code_reports_listed_literals_in_verb_code(monkeypatch, tmp_path):
    assert run(monkeypatch, tmp_path, "--code").exit_code == 2
    ids = tmp_path / "ids.tsv"
    ids.write_text("sound\t5\nslice\ta.m4a#02\n")
    r = run(monkeypatch, tmp_path, "--ids", str(ids), "--code", "--kind", "code")
    assert r.exit_code == 1, r.output
    # 5 is a literal on line 1; #5 is an object and 50 another number; line 3 is a comment string, not equal to the slice.
    assert r.output.splitlines() == [
        '#2 "obj2":[0] play:1  code  5  code',
        '#2 "obj2":[0] play:2  code  "a.m4a#02"  code',
    ]


def test_cli_summary_counts_kinds_and_distinct_values(monkeypatch, tmp_path):
    r = run(monkeypatch, tmp_path, "--summary", "--skip", "cache")
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0] == "by kind and context"
    assert "  sound  snd-property  2" in lines
    assert "  group  social  1" in lines
    assert lines[lines.index("distinct values by kind") + 1 : lines.index("by property (top 40)")] == [
        "  sound  5",
        "  group  2",
        "  path  1",
        "  slice  1",
    ]
    assert "  x_snd  1" in lines
    assert lines[-2:] == ["skipped properties", "  cache  1"]
