import re
from io import StringIO
from pathlib import Path

import pytest
from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import CLEAR, MooDatabase, MooError, MooObject, ObjNum, Property, Verb
from lambdamoo_db.inspection import (
    LookupFailed,
    added_verb_lines,
    all_properties,
    find_slot,
    find_verb,
    format_value,
    lookup_property,
    property_value,
    resolve_object,
    split_ref,
    verb_args,
    verb_perms,
    verbcasecmp,
)
from lambdamoo_db.reader import Reader, load

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"


@pytest.fixture(scope="module")
def db():
    return load(str(TOASTCORE))


@pytest.mark.parametrize(
    "verb,word,expected",
    [
        ("GET", "get", True),
        ("status_redirect*ion", "status_redirect", True),
        ("status_redirect*ion", "status_redirecti", True),
        ("status_redirect*ion", "status_redirection", True),
        ("status_redirect*ion", "status_redirec", False),
        ("status_redirect*ion", "status_redirectionx", False),
        ("foo*", "foobar", True),
        ("foo*", "fo", False),
        ("*", "anything", True),
        ("html_robots.txt", "html_robots.txt", True),
        # Aliases are space-separated; any one may match.
        ("l*ook examine ex", "ex", True),
        ("l*ook examine ex", "loo", True),
        ("l*ook examine ex", "exam", False),
        # A later star keeps the verbcasecmp() state machine semantics.
        ("a*b*c", "abc", True),
        ("a*b*", "abzz", True),
        ("a*b*c", "ac", False),
        ("", "", False),
    ],
)
def test_verbcasecmp(verb, word, expected):
    assert verbcasecmp(verb, word) is expected


def test_verb_args_and_perms():
    v = Verb("GET HEAD", ObjNum(2), 173, -1, 852)
    assert verb_args(v) == "this none this"
    assert verb_perms(v) == "rxd"
    assert verb_args(Verb("look", ObjNum(2), 1 | (1 << 4), -2, 1)) == "any any none"
    assert verb_args(Verb("put", ObjNum(2), 0, 3, 1)) == "none in/inside/into none"


def test_format_value():
    assert format_value(["a\"b\\", ObjNum(5), MooError(3), {"k": 1.5}, True, CLEAR]) == '{"a\\"b\\\\", #5, E_PERM, ["k" -> 1.5], true, clear}'
    assert format_value("x" * 50, 10) == '"xxxxxx...'


def test_split_ref():
    assert split_ref("$httpd:GET") == ("$httpd", ":GET")
    assert split_ref("#852.port") == ("#852", ".port")
    assert split_ref("12:3") == ("12", ":3")
    assert split_ref("#-1") == ("#-1", "")
    with pytest.raises(LookupFailed):
        split_ref("httpd:GET")


def test_resolve_dollar_name(db):
    assert resolve_object(db, "$string_utils").id == 20
    assert resolve_object(db, "$STRING_UTILS").id == 20
    assert resolve_object(db, "#20").id == 20
    with pytest.raises(LookupFailed):
        resolve_object(db, "#999999")
    with pytest.raises(LookupFailed):
        resolve_object(db, "$no_such_name_here")


def test_cli_nested_object_references(db, monkeypatch):
    """Namespace chains work for object, program and final-property reads."""
    import lambdamoo_db.cli as cli

    monkeypatch.setattr(cli, "open_indexed", lambda *_: db)
    runner = CliRunner()
    # #0.string_utils -> #20; builtin owner -> #2. No new fixture props.
    for args, expected in [
        (["obj", "$string_utils.owner"], '#2'),
        (["prop", "$string_utils.owner.name"], '#2.name'),
        (["code", "$string_utils.owner:look_self"], '@program'),
        (["children", "$string_utils.owner"], ''),
    ]:
        result = runner.invoke(moodb, ["--db", str(TOASTCORE), *args])
        assert result.exit_code == 0, result.output
        assert expected in result.output
    result = runner.invoke(moodb, ["--db", str(TOASTCORE), "obj", "$string_utils.name"])
    assert result.exit_code != 0
    assert "not an object" in result.output


def test_nested_resolver_rejects_missing_and_malformed_references(db):
    assert resolve_object(db, "$string_utils.owner").id == 2
    assert resolve_object(db, "#0.STRING_UTILS.owner").id == 2
    for ref in ("$string_utils.owner.", "$string_utils..owner", "#20:look", "#20junk"):
        with pytest.raises(LookupFailed):
            resolve_object(db, ref)
    with pytest.raises(LookupFailed, match="not an object"):
        resolve_object(db, "$string_utils.name")


def test_cli_nested_properties_preserve_literal_dotted_names(monkeypatch):
    import lambdamoo_db.cli as cli

    synthetic = MooDatabase()
    definitions = [
        (0, [], ["namespace"], [ObjNum(4)]),
        (1, [], ["field.with.dot", "member", "member.name"], ["literal", ObjNum(2), "literal precedence"]),
        (2, [], ["field.with.dot"], ["nested literal"]),
        (4, [1], [], [CLEAR, CLEAR, CLEAR]),
    ]
    for num, parents, own, values in definitions:
        obj = MooObject(num, f"obj{num}", 0, 0, -1, [ObjNum(p) for p in parents])
        obj.properties = [Property(n, v, 0, 5) for n, v in zip(own + [None] * (len(values) - len(own)), values)]
        obj.propdefs_count = len(own)
        synthetic.objects[num] = obj
    for obj in synthetic.objects.values():
        Reader(StringIO()).process_propnames(synthetic, obj)
    monkeypatch.setattr(cli, "open_indexed", lambda *_: synthetic)
    runner = CliRunner()
    for ref, expected in [
        ("$namespace.FIELD.WITH.DOT", '"literal"'),
        ("$namespace.member.field.with.dot", '"nested literal"'),
        ("$namespace.member.name", '"literal precedence"'),
        ("$namespace.member.owner", "#0"),
    ]:
        result = runner.invoke(moodb, ["--db", str(TOASTCORE), "prop", ref])
        assert result.exit_code == 0, result.output
        assert expected in result.output
    assert resolve_object(synthetic, "$namespace.member").id == 2


def test_find_verb_by_name_index_and_inheritance(db):
    su = resolve_object(db, "$string_utils")
    hit = find_verb(db, su, "from_list")
    assert (hit.obj.id, hit.verb.name) == (20, "from_list")
    assert find_verb(db, su, str(hit.index)).verb is hit.verb
    with pytest.raises(LookupFailed):
        find_verb(db, su, "²")  # a Unicode digit is a name, not an index

    # A verb defined only on an ancestor is found from the child, and reported on the ancestor.
    sysobj = db.objects[0]
    root = db.objects[1]
    inherited = next(v for v in root.verbs if not any(verbcasecmp(own.name, a) for own in sysobj.verbs for a in v.name.split()))
    wanted = inherited.name.split()[0].replace("*", "")
    hit = find_verb(db, sysobj, wanted)
    assert hit.obj.id == 1
    with pytest.raises(LookupFailed):
        find_verb(db, sysobj, wanted, inherited=False)


def test_find_verb_falls_back_to_waif_verb_spelling(db):
    su = resolve_object(db, "$string_utils")
    plain = find_verb(db, su, "from_list").verb
    waif_verb = Verb(":only_on_waifs", ObjNum(2), 173, -1, su.id)
    su.verbs.append(waif_verb)
    try:
        assert find_verb(db, su, "only_on_waifs").verb is waif_verb
        assert find_verb(db, su, ":only_on_waifs").verb is waif_verb
        assert find_verb(db, su, "from_list").verb is plain
    finally:
        su.verbs.remove(waif_verb)


def test_property_value_follows_clear(db):
    for obj in db.objects.values():
        cleared = [p for p in obj.properties[obj.propdefs_count:] if p.value is CLEAR and isinstance(p.propertyName, str)]
        if obj.parents and cleared:
            break
    else:
        pytest.skip("no inherited clear property in fixture")
    name = cleared[0].propertyName
    expected = next(
        p.value for a in db.ancestors(obj)[1:] for p in a.properties if p.propertyName == name and p.value is not CLEAR
    )
    assert property_value(db, obj, name) == expected
    assert property_value(db, obj, name.upper()) == expected


def test_builtin_properties_match_toaststunt(db):
    wizard = db.objects[2]
    assert property_value(db, wizard, "name") == wizard.name
    assert property_value(db, wizard, "wizard") == 1
    assert property_value(db, wizard, "Programmer") == 1
    assert property_value(db, db.objects[20], "wizard") == 0
    assert property_value(db, db.objects[20], "owner") == ObjNum(2)
    # parents/children are builtin functions, not properties.
    with pytest.raises(LookupFailed):
        property_value(db, wizard, "parents")


def test_slot_lookup_agrees_with_reader_names(db):
    """find_slot() places every named property at the slot where the reader put that name."""
    for obj in db.objects.values():
        for i, p in enumerate(obj.properties):
            if isinstance(p.propertyName, str):
                assert find_slot(db, obj, p.propertyName).index == i, (obj.id, p.propertyName)


def multi_parent_db() -> MooDatabase:
    """#3 has parents #1 and #2; #4 is a child of #3 with every slot clear."""
    db = MooDatabase()

    def add(num, parents, own, values):
        o = MooObject(num, f"obj{num}", 0, 0, -1, [ObjNum(p) for p in parents])
        names = own + [None] * (len(values) - len(own))
        o.properties = [Property(n, v, 0, 5) for n, v in zip(names, values)]
        o.propdefs_count = len(own)
        db.objects[num] = o

    add(0, [], [], [])
    add(1, [], ["a1"], ["A-value"])
    add(2, [], ["b1"], ["B-value"])
    add(3, [1, 2], ["c1"], ["C-value", CLEAR, "C-b1"])
    add(4, [3], [], [CLEAR, CLEAR, CLEAR])
    for o in db.objects.values():
        Reader(StringIO()).process_propnames(db, o)
    return db


def test_reader_names_inherited_slots_of_multi_parent_objects():
    db = multi_parent_db()
    assert [p.propertyName for p in db.objects[3].properties] == ["c1", "a1", "b1"]
    assert [p.propertyName for p in db.objects[4].properties] == ["c1", "a1", "b1"]


def test_clear_follows_the_parent_that_inherits_the_definer():
    db = multi_parent_db()
    child = db.objects[4]
    assert property_value(db, child, "b1") == "C-b1"  # #4 clear -> #3's own value
    hit = lookup_property(db, child, "a1")  # #4 clear -> #3 clear -> #1
    assert (hit.value, hit.definer.id, hit.value_from.id) == ("A-value", 1, 1)
    assert [(h.name, h.value) for h in all_properties(db, child)] == [("c1", "C-value"), ("a1", "A-value"), ("b1", "C-b1")]


def run(*args):
    return CliRunner().invoke(moodb, ["--db", str(TOASTCORE), "--no-cache", *args])


def _verb_db(verbs_by_obj) -> MooDatabase:
    db = MooDatabase()
    for num, verbs in verbs_by_obj.items():
        o = MooObject(num, f"obj{num}", 0, 0, -1, [])
        for name, code in verbs:
            v = Verb(name, ObjNum(2), 173, -1, 1)
            v.code = code
            o.verbs.append(v)
        db.objects[num] = o
    return db


def test_added_verb_lines_reports_only_what_the_old_verb_lacks():
    old = _verb_db({
        1: [("move", ["a = 1;", "`b() ! ANY';", "return a;"]), ("keep", ["try", "x();", "except (ANY)", "endtry"])],
    })
    new = _verb_db({
        # "helper" is inserted first, so "move" and "keep" change index and must still pair by name.
        1: [
            ("helper", ["try", "y();", "except e (ANY)", "endtry"]),
            ("move", ["a = 1;", "`b() ! ANY';", "  `c() ! E_INVARG => 0';", "`b() ! ANY';", "return a;"]),
            ("keep", ["x();", "try", "except (ANY)", "endtry"]),
        ],
        2: [("fresh", ["return `d() ! ANY';"])],
    })
    hits = [(h.obj.id, h.verb.name, h.lineno, h.line) for h in added_verb_lines(old, new)]
    assert (1, "move", 3, "  `c() ! E_INVARG => 0';") in hits
    # A second copy of a line the old verb had once is an addition.
    assert [h for h in hits if h[1] == "move"] == [(1, "move", 3, "  `c() ! E_INVARG => 0';"), (1, "move", 4, "`b() ! ANY';")]
    # Every line of a verb or object the old dump lacks is reported.
    assert len([h for h in hits if h[1] == "helper"]) == 4
    assert (2, "fresh", 1, "return `d() ! ANY';") in hits
    # "keep" only moved x(); above try: one line reads as added, the try/except lines do not.
    assert [h[3] for h in hits if h[1] == "keep"] == ["x();"]

    catches = re.compile(r"^(try|except\b.*|finally)$|`[^']*!")
    caught = [(h.obj.id, h.verb.name, h.lineno) for h in added_verb_lines(old, new, catches)]
    assert caught == [(1, "helper", 1), (1, "helper", 3), (1, "move", 3), (1, "move", 4), (2, "fresh", 1)]


def test_cli_added(monkeypatch, tmp_path):
    import lambdamoo_db.cli as cli

    old = _verb_db({0: [], 1: [("move", ["a = 1;"])]})
    new = _verb_db({0: [], 1: [("move", ["a = 1;", "`b() ! ANY';"]), ("fresh", ["try"])]})
    old_path = tmp_path / "old.db"
    old_path.write_text("")
    monkeypatch.setattr(cli, "open_indexed", lambda path, *_: old if Path(path) == old_path else new)
    monkeypatch.setattr(cli, "load", lambda path: old if Path(path) == old_path else new)
    r = CliRunner().invoke(moodb, ["--db", str(TOASTCORE), "added", str(old_path)])
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == [
        "#1:[0] move:2: `b() ! ANY';",
        "#1:[1] fresh:1: try  (new verb)",
        "2 added lines in 2 verbs",
    ]
    r = CliRunner().invoke(moodb, ["--db", str(TOASTCORE), "added", "-l", str(old_path), "^try$"])
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == ['   1  #1 "obj1":[1] "fresh"  (new verb)', "1 added lines in 1 verbs"]

    for option in ([], ["--no-cache"]):
        r = CliRunner().invoke(moodb, ["--db", str(TOASTCORE), *option, "added", "-F", "-i", str(old_path), "TRY"])
        assert r.exit_code == 0, r.output
        assert "fresh:1: try  (new verb)" in r.output
        r = CliRunner().invoke(moodb, ["--db", str(TOASTCORE), *option, "added", str(old_path), "("])
        assert r.exit_code == 2 and "Invalid value for PATTERN" in r.output


def test_added_pairs_duplicate_verbs_by_occurrence_and_marks_the_extra_one():
    old = _verb_db({1: [("same", ["return 1;"]), ("same", ["return 2;"])]})
    new = _verb_db({1: [("same", ["return 1;"]), ("same", ["return 3;"]), ("same", ["return 4;"])]})
    assert [(h.index, h.line, h.is_new) for h in added_verb_lines(old, new)] == [
        (1, "return 3;", False), (2, "return 4;", True),
    ]


def test_added_ignores_indentation_and_permission_changes_but_matches_argument_specs():
    old = _verb_db({1: [("keep", ["  return 1;  "]), ("change", ["return 2;"])]})
    new = _verb_db({1: [("keep", ["return 1;"]), ("change", ["return 2;"])]})
    new.objects[1].verbs[0].perms ^= 1
    new.objects[1].verbs[1].preps = 0
    assert [(h.index, h.is_new) for h in added_verb_lines(old, new)] == [(1, True)]


def test_added_existing_unprogrammed_verb_is_not_new():
    old = _verb_db({1: [("empty", None)]})
    new = _verb_db({1: [("empty", ["return 1;"])]})
    assert [(h.line, h.is_new) for h in added_verb_lines(old, new)] == [("return 1;", False)]


def test_cli_code_prop_grep_find_obj():
    r = run("code", "-n", "$string_utils:from_list")
    assert r.exit_code == 0, r.output
    assert r.output.startswith('@program #20 $string_utils "string utilities":[5] "from_list"  this none this  rxd')
    assert "   1  " in r.output and r.output.rstrip().endswith(".")

    r = run("prop", "$string_utils.name", "0.string_utils", "#20.DESCRIPTION", "#2.wizard")
    lines = r.output.splitlines()
    assert lines[:2] == ['#20.name = "string utilities"', "#0.string_utils = #20"]
    assert lines[2].startswith("#20.description (defined on #1) = ")
    assert lines[3] == "#2.wizard = 1"

    r = run("grep", "-l", "-F", "tostr(@thelist)", "-o", "$string_utils")
    assert r.exit_code == 0 and '"from_list"' in r.output

    r = run("find", "string_utils")
    assert '#20 $string_utils "string utilities"' in r.output

    r = run("obj", "0")
    assert r.exit_code == 0
    assert "##" not in r.output
    assert '  parents   #1 $root_class "Root Class"' in r.output


def test_cli_code_bare_ref_prints_every_verb():
    r = run("code", "$string_utils")
    assert r.exit_code == 0, r.output
    assert r.output.count("@program #20 ") == 79


def test_cli_grep_context_and_bad_regex():
    r = run("grep", "-F", "-C", "1", "tostr(@thelist)", "-o", "20")
    assert r.output.splitlines() == [
        '#20:[5] from_list-4- if (separator == "")',
        "#20:[5] from_list:5: return tostr(@thelist);",
        "#20:[5] from_list-6- elseif (thelist)",
    ]
    r = run("grep", "foo(")
    assert r.exit_code == 2 and "Invalid value for PATTERN" in r.output


def test_cli_new_commands():
    r = run("info")
    assert "objects   127 (+0 anonymous)" in r.output and "tasks     1 queued" in r.output

    r = run("props", "$string_utils")
    assert r.exit_code == 0
    assert "== #1 $root_class" in r.output and "(clear, from #78)" in r.output

    r = run("find", "--verb", "from_list")
    assert '#33 $seq_utils "sequence utilities":[8] "from_list"' in r.output

    r = run("find", "--prop", "STRING_UTILS")
    assert r.output == '#0 $sysobj "The System Object".string_utils\n'

    r = run("refs", "$string_utils")
    lines = r.output.splitlines()
    assert lines[0] == '#0 $sysobj "The System Object".string_utils'
    assert any(":[0] do_login_command:15: " in line for line in lines)

    r = run("children", "$generic_utils")
    assert '#20 $string_utils "string utilities"' in r.output
    assert run("children", "-r", "1").output.count("\n") > run("children", "1").output.count("\n")

    r = run("players")
    assert '#2 "Wizard"  player programmer wizard' in r.output

    r = run("tasks")
    assert r.output.startswith("queued 419331400  2022-12-17 08:00:01Z  #79:schedule_measurement_task")


def test_cli_lookup_errors_exit_nonzero():
    r = run("obj", "#999999")
    assert r.exit_code == 1 and "does not exist" in r.output
    r = run("code", "$string_utils.name")
    assert r.exit_code == 1 and "not an object" in r.output
    r = run("obj", "#20.name")
    assert r.exit_code == 1 and "not an object" in r.output


def test_cli_help_and_missing_db(monkeypatch):
    monkeypatch.delenv("MOODB", raising=False)
    r = CliRunner().invoke(moodb, ["grep", "--help"])
    assert r.exit_code == 0 and "--context" in r.output
    r = CliRunner().invoke(moodb, ["info"])
    assert r.exit_code == 2 and "pass --db DUMP or set MOODB" in r.output
