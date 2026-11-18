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


def test_cli_ids_match_paths_and_slices_as_the_game_resolves_them(monkeypatch, tmp_path):
    # #6128:resolve looks a string up URL-decoded and lowercased, and accepts a slice number with or without leading zeros.
    db = sound_db()
    held = db.objects[2]
    held.properties[1].value = [{"group": 7}, "A.M4A#2"]
    held.properties[5].value = "Doors/Creak%20Slow.OGG"
    held.verbs[0].code = ['x = "A.m4a#002";', 'y = "b.m4a#02";']
    monkeypatch.setattr(cli, "_db", lambda _: (db, {}))
    ids = tmp_path / "ids.tsv"
    ids.write_text("slice\ta.m4a#02\npath\tdoors/creak slow.ogg\n")
    r = CliRunner().invoke(cli.moodb, ["sound-refs", "--ids", str(ids), "--code"])
    assert r.exit_code == 1, r.output
    assert r.output.splitlines() == [
        '#2 "obj2".y_snd[2]  slice  "A.M4A#2"  string',
        '#2 "obj2".path_note  path  "Doors/Creak%20Slow.OGG"  string',
        '#2 "obj2":[0] play:1  code  "A.m4a#002"  code',
    ]


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


def test_cli_output_writes_the_lines_to_a_file_and_reports_the_count(monkeypatch, tmp_path):
    ids = tmp_path / "ids.tsv"
    ids.write_text("sound\t5\nslice\ta.m4a#02\n")
    for args in (["--skip", "cache"], ["--kind", "group", "--format", "jsonl"], ["--summary", "--skip", "cache"],
                 ["--ids", str(ids)]):
        plain = run(monkeypatch, tmp_path, *args)
        assert plain.stdout != ""
        out = tmp_path / "refs.txt"
        r = run(monkeypatch, tmp_path, *args, "--output", str(out))
        # The same exit status and stderr; stdout holds only the count and the file.
        assert (r.exit_code, r.stderr) == (plain.exit_code, plain.stderr), r.output
        assert out.read_bytes() == plain.stdout.encode("utf-8")
        assert b"\r" not in out.read_bytes()
        assert r.stdout == f"{len(plain.stdout.splitlines())} lines: {out}\n"


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


FRIDGE = 20
SCENE_EVENT = 14


def nested_db() -> MooDatabase:
    """sound_db plus #20, holding sound ids where the game reads them outside ``*_snd`` properties.

    Each shape has a reader in the game: ``electronics_profile`` keys
    (#53020:storage_notice line 7, #13232:sync_sound line 12),
    ``interaction_sounds`` (#4031:interaction_sound line 8), a ``$scene_event``
    waif's ``sounds`` (#10205:make_sound line 1), ``tone_sound_id``
    (#53614:apply line 19), ``login_music_id`` (#2585:login_music_sound line 2),
    an effect step's ``params["sound_id"]`` (#2585:step_playback line 23), a
    build plan's ``settings["sounds"]`` (#29076:check line 104) and message
    markers (#2585:msg lines 3 and 5).
    """
    db = sound_db()
    own = ["electronics_profile", "interaction_sounds", "tone_sound_id", "login_music_id", "steps", "plan",
           "greet_msg", "old_msg", "stock", "events"]
    values = [
        {"door_open_snd": 43875, "volume": 15, "idle_snd": [{"group": 8}, 44000]},
        {"": 301, "The lid creaks.": [302, 303]},
        304,
        305,
        [{"verb": "play", "params": {"sound_id": 306, "duration_s": 4}}],
        {"things": [{"settings": {"sounds": {"open": 307}, "mass_g": 900}}]},
        "You wave. $sndid, 308 and smile",
        "Bye $snd,doors/slam.m4a now",
        43875,
        [WaifReference(3)],
    ]
    o = MooObject(FRIDGE, "fridge", 0, 0, -1, [])
    o.properties = [Property(n, v, 0, 5) for n, v in zip(own, values)]
    o.propdefs_count = len(own)
    db.objects[FRIDGE] = o
    cls = MooObject(SCENE_EVENT, "scene_event", 0, 0, -1, [])
    cls.properties = [Property(":sounds", [], 0, 5), Property(":volume", 0, 0, 5)]
    cls.propdefs_count = 2
    db.objects[SCENE_EVENT] = cls
    db.waifs[3] = Waif(SCENE_EVENT, 2, [(0, [309, "a/b.ogg"]), (1, 310)], 2)
    return db


NESTED = {
    (FRIDGE, '.electronics_profile["door_open_snd"]', "sound", 43875, "snd-key"),
    (FRIDGE, '.electronics_profile["idle_snd"][1]["group"]', "group", 8, "snd-key"),
    (FRIDGE, '.electronics_profile["idle_snd"][2]', "sound", 44000, "snd-key"),
    (FRIDGE, '.interaction_sounds[""]', "sound", 301, "sounds"),
    (FRIDGE, '.interaction_sounds["The lid creaks."][1]', "sound", 302, "sounds"),
    (FRIDGE, '.interaction_sounds["The lid creaks."][2]', "sound", 303, "sounds"),
    (FRIDGE, ".tone_sound_id", "sound", 304, "sound-id"),
    (FRIDGE, ".login_music_id", "sound", 305, "sound-id"),
    (FRIDGE, '.steps[1]["params"]["sound_id"]', "sound", 306, "sound-id"),
    (FRIDGE, '.plan["things"][1]["settings"]["sounds"]["open"]', "sound", 307, "sounds"),
    (FRIDGE, ".greet_msg", "sound", 308, "marker"),
    (FRIDGE, ".old_msg", "path", "doors/slam.m4a", "marker"),
    (FRIDGE, f".events[1]<waif #{SCENE_EVENT}>.sounds[1]", "sound", 309, "sounds"),
    (FRIDGE, f".events[1]<waif #{SCENE_EVENT}>.sounds[2]", "path", "a/b.ogg", "string"),
}


def test_sound_ids_under_snd_keys_and_the_other_names_the_game_reads():
    found = refs(nested_db())
    assert len(found) == len(set(found))
    # Not reported: volume 15, duration_s 4, mass_g 900, the waif's volume 310 and stock = 43875.
    assert set(found) == EXPECTED | NESTED


def test_bare_reports_a_listed_id_no_rule_claimed():
    found = refs(nested_db(), bare={43875, 15})
    assert len(found) == len(set(found))
    assert set(found) == EXPECTED | NESTED | {
        (FRIDGE, '.electronics_profile["volume"]', "sound", 15, "bare"),
        (FRIDGE, ".stock", "sound", 43875, "bare"),
    }


def test_cli_ids_find_the_fridge_door_sound(monkeypatch, tmp_path):
    db = nested_db()
    monkeypatch.setattr(cli, "_db", lambda _: (db, {}))
    ids = tmp_path / "ids.tsv"
    ids.write_text("sound\t43875\nsound\t308\npath\tdoors/slam.m4a\n")
    r = CliRunner().invoke(cli.moodb, ["sound-refs", "--ids", str(ids)])
    assert r.exit_code == 1, r.output
    assert r.output.splitlines() == [
        '#20 "fridge".electronics_profile["door_open_snd"]  sound  43875  snd-key',
        '#20 "fridge".greet_msg  sound  308  marker',
        '#20 "fridge".old_msg  path  "doors/slam.m4a"  marker',
    ]
    assert CliRunner().invoke(cli.moodb, ["sound-refs", "--bare"]).exit_code == 2
    r = CliRunner().invoke(cli.moodb, ["sound-refs", "--ids", str(ids), "--bare"])
    assert r.exit_code == 1, r.output
    assert r.output.splitlines()[-1] == '#20 "fridge".stock  sound  43875  bare'
    assert len(r.output.splitlines()) == 4

    # --except-ids takes the rows a removal kept out of the id set.
    kept = tmp_path / "kept.tsv"
    kept.write_text("sound\t43875\n")
    assert CliRunner().invoke(cli.moodb, ["sound-refs", "--except-ids", str(kept)]).exit_code == 2
    r = CliRunner().invoke(cli.moodb, ["sound-refs", "--ids", str(ids), "--except-ids", str(kept), "--bare"])
    assert r.exit_code == 1, r.output
    assert r.output.splitlines() == [
        '#20 "fridge".greet_msg  sound  308  marker',
        '#20 "fridge".old_msg  path  "doors/slam.m4a"  marker',
    ]
