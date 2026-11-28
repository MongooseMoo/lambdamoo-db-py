from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lambdamoo_db.audit import AuditRuleError, audit_database, format_audit_text, load_rules, validate_rules
from lambdamoo_db.cli import moodb
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum, Property, Verb
from lambdamoo_db.enums import ObjectFlags
from lambdamoo_db.inspection import VERB_EXEC

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"

# Perms encoding: r=1, w=2, x=4, d=8; dobj/iobj bits: this=2 (<<4), none=0 (<<6), any=1
_THIS_NONE_THIS = 0x1 | 0x4 | 0x8 | (2 << 4) | (2 << 6)  # rxd, this none this
_THIS_NONE_NONE = 0x1 | 0x8 | (2 << 4) | (0 << 6)        # rd,  this none none
_RD_THIS_NONE_THIS = 0x1 | 0x8 | (2 << 4) | (2 << 6)     # rd,  this none this


def _make_obj(
    num: int,
    name: str,
    *,
    owner: int = 2,
    flags: int = 0,
    parents: list[int] | None = None,
    children: list[int] | None = None,
    props: dict[str, object] | None = None,
    verbs: list[Verb] | None = None,
) -> MooObject:
    o = MooObject(num, name, flags, owner, -1, [ObjNum(p) for p in (parents or [])])
    o.children = [ObjNum(c) for c in (children or [])]
    if props:
        o.properties = [Property(k, v, owner, 5) for k, v in props.items()]
        o.propdefs_count = len(props)
    o.verbs = list(verbs or [])
    return o


def _verb(name: str, code: list[str], *, owner: int = 2, perms: int = _THIS_NONE_THIS, preps: int = -1, obj_id: int = 0) -> Verb:
    v = Verb(name, owner, perms, preps, obj_id)
    v.code = list(code)
    return v


def _sample_pair() -> tuple[MooDatabase, MooDatabase]:
    old = MooDatabase()
    new = MooDatabase()

    def setup_common(db: MooDatabase) -> None:
        db.objects[0] = _make_obj(
            0, "sysobj",
            props={"root": ObjNum(1), "core_room": ObjNum(10), "helper": ObjNum(20)},
        )
        db.objects[1] = _make_obj(1, "Root", flags=int(ObjectFlags.FERTILE))
        db.objects[2] = _make_obj(2, "Wiz", flags=int(ObjectFlags.USER | ObjectFlags.PROGRAMMER | ObjectFlags.WIZARD))
        db.objects[3] = _make_obj(3, "Builder", flags=int(ObjectFlags.USER | ObjectFlags.PROGRAMMER))

    setup_common(old)
    setup_common(new)

    # #10 core_room: existing object in both dumps, edited in new
    old.objects[10] = _make_obj(
        10, "Core Room", parents=[1], props={"creation_time": 100},
        verbs=[
            _verb("say", [
                '"Speak aloud.";',
                '"Doc line two.";',
                '"Doc line three.";',
                'return 1;',
                '"Last modified Mon Nov 10 01:00:00 2026 UTC by Wiz (#2@Test).";',
            ]),
            _verb("trailer_only", [
                '"Only timestamp changes.";',
                'return 0;',
                '"Last modified Mon Nov 10 01:00:00 2026 UTC by Wiz (#2@Test).";',
            ]),
        ],
    )
    new.objects[10] = _make_obj(
        10, "Core Room", parents=[1], props={"creation_time": 100},
        verbs=[
            _verb("say", [
                '"Speak aloud.";',
                'x = $optional_mod:hook(this);',
                'y = #42:ping();',
                'z = "string with #999 ignored";',
                'ok = #0.root;',
                'return x;',
                '"Last modified Tue Nov 24 05:00:00 2026 UTC by bot42 (#900@Test).";',
            ]),
            _verb("trailer_only", [
                '"Only timestamp changes.";',
                'return 0;',
                '"Last modified Tue Nov 24 05:00:00 2026 UTC by Wiz (#2@Test).";',
            ]),
        ],
    )

    # #20 helper: existing object, new verbs added (catches, security, command, test, duplicate)
    dup_body = [
        '"Dup line 1.";',
        '"Dup line 2.";',
        '"Dup line 3.";',
        "a = args[1];",
        "b = a + 1;",
        "c = b * 2;",
        "d = c - 3;",
        "return d;",
    ]
    old.objects[20] = _make_obj(20, "Helper", parents=[1], props={"creation_time": 100})
    new.objects[20] = _make_obj(
        20, "Helper", parents=[1], props={"creation_time": 100},
        verbs=[
            _verb("risky_mutator", [
                '"Deletes a target without checking caller.";',
                "recycle(args[1]);",
                "return 1;",
                '"Last modified Wed Nov 25 10:00:00 2026 UTC by Wiz (#2@Test).";',
            ], owner=2, perms=_THIS_NONE_THIS),
            _verb("safe_mutator", [
                '"Deletes a target after checking caller_perms().";',
                '"Only wizards may call this.";',
                '"Returns 1 after recycling.";',
                "caller_perms().wizard || raise(E_PERM);",
                "recycle(args[1]);",
                "return 1;",
            ], owner=2, perms=_THIS_NONE_THIS),
            _verb("non_exec_mutator", [
                '"Non-executable verb should not trigger security check.";',
                "recycle(args[1]);",
            ], owner=2, perms=_RD_THIS_NONE_THIS),
            _verb("broad_catch", [
                '"Swallows every error.";',
                'x = `args[1]:foo() ! ANY => 0\';',
                'try',
                '  y = args[1]:bar();',
                'except (E_INVARG)',
                '  y = 0;',
                'endtry',
                'return this:event_ping(x + y);',
            ], owner=3),
            _verb("sniff smell", [
                '"Custom smell command.";',
                'player:tell("You sniff it.");',
            ], owner=3, perms=_THIS_NONE_NONE),
            _verb("dup_work", dup_body, owner=3),
            _verb("test_something", [
                '"Test with raw create and catch.";',
                'o = create($root);',
                'e = `o:bad() ! E_PERM\';',
                'return $pbt:check(this, "prop", {}, []);',
            ], owner=2),
        ],
    )

    # #30 newly added non-fertile generic with .proposed, no $name, duplicate verb body, and zz_ temp verb
    new.objects[30] = _make_obj(
        30, "generic widget", parents=[1], flags=0, props={"creation_time": 200, "proposed": 1},
        verbs=[
            _verb("dup_work", dup_body, owner=3),
            _verb("zz_scratch", ['"Temp helper.";', "return 1;"], owner=2),
        ],
    )
    return old, new


def test_builtin_checks_without_rules_file():
    old, new = _sample_pair()
    rep = audit_database(new, old)

    assert rep["mode"] == "diff"
    assert rep["rules"] == []
    assert rep["overview"]["verbs_changed_trailer_only"] == 1
    assert rep["overview"]["verbs_changed"] == 1

    # raw-refs: flags #42, skips #999 in string and #0
    raw = rep["builtins"]["raw-refs"]
    assert raw["count"] == 1
    assert raw["items"][0]["targets"] == [42]
    assert raw["items"][0]["verb"] == "say"

    # catches: 1 prod ANY, 1 prod specific, 1 test catch
    catches = rep["builtins"]["catches"]
    assert catches["prod_any_count"] == 1
    assert catches["prod_specific_count"] == 1
    assert catches["test_catch_count"] == 1

    # docstrings: revised verb `say` dropped from 3 doc lines to 1 -> caught!
    docs = rep["builtins"]["docstrings"]
    assert docs["counts"]["changed_prod"] == {"1-2": 1}
    assert any(r["verb"] == "say" and r["change"] == "changed" and r["doc_lines"] == 1 for r in docs["prod_failures"])
    assert not any(r["verb"] == "safe_mutator" for r in docs["prod_failures"])

    # security: flags risky_mutator, ignores safe_mutator and non_exec_mutator
    sec = rep["builtins"]["security"]
    assert [r["verb"] for r in sec["items"]] == ["risky_mutator"]
    assert sec["items"][0]["mutators"] == ["recycle"]

    # duplicates: dup_work appears on #20 and #30
    dups = rep["builtins"]["duplicates"]
    assert dups["group_count"] == 1
    assert dups["groups"][0]["count"] == 2


def test_declarative_rules_line_verb_boundary_and_object():
    old, new = _sample_pair()
    rules = {
        "version": 1,
        "name": "test-rules",
        "min_doc_lines": 3,
        "test_verb_prefixes": ["test_"],
        "author_groups": [{"pattern": "^bot\\d+$", "label": "in-moo botNNN"}],
        "line_rules": [
            {"id": "direct-event", "category": "events", "scope": "prod", "match": r":event_\w+\s*\("},
            {"id": "pbt-metric", "category": "testing", "mode": "metric", "count_by": "verb", "scope": "test", "match": r"\$pbt:"},
            {"id": "raw-create-test", "category": "testing", "scope": "test", "match": r"(?<![:_A-Za-z0-9])create\s*\("},
        ],
        "verb_rules": [
            {"id": "custom-smell", "category": "commands", "scope": "prod", "change": "added", "args_not": "this none this", "name_match": r"\b(smell|sniff)\b"},
            {"id": "cmd-no-msg", "category": "commands", "scope": "prod", "change": "added", "args_not": "this none this", "require": r"player:tell\(", "without": r"_msg\b"},
            {"id": "temp-zz", "category": "generics", "scope": "prod", "change": "added", "name_match": r"^zz_"},
        ],
        "boundary_rules": [
            {"id": "core-boundary", "category": "events", "objects": ["$core_room"], "allowed_refs": ["helper"]},
        ],
        "object_rules": [
            {
                "id": "generic-hygiene",
                "category": "generics",
                "when_name_contains": ["generic"],
                "require_fertile": True,
                "require_dollar_name": True,
                "forbid_own_properties": ["proposed"],
            }
        ],
    }

    rep = audit_database(new, old, rules=rules)
    assert rep["rules_name"] == "test-rules"
    by_id = {r["id"]: r for r in rep["rules"]}

    assert by_id["direct-event"]["count"] == 1
    assert by_id["pbt-metric"]["count"] == 1 and by_id["pbt-metric"]["items"] == []
    assert by_id["raw-create-test"]["count"] == 1
    assert by_id["custom-smell"]["count"] == 1
    assert by_id["cmd-no-msg"]["count"] == 1
    assert by_id["temp-zz"]["count"] == 1
    assert by_id["core-boundary"]["count"] == 1
    assert by_id["core-boundary"]["items"][0]["dollar_ref"] == "$optional_mod"
    assert by_id["generic-hygiene"]["count"] == 1
    assert set(by_id["generic-hygiene"]["items"][0]["issues"]) == {"not_fertile", "no_dollar_name", "has_proposed_prop"}

    # author_groups normalized bot42
    author_labels = {row["author"] for row in rep["overview"]["authors"]}
    assert "in-moo botNNN" in author_labels

    # Category filter selects only matching rules and excludes unselected builtins
    filtered = audit_database(new, old, rules=rules, checks=["events"])
    assert filtered["builtins"] == {}
    assert {r["id"] for r in filtered["rules"]} == {"direct-event", "core-boundary"}

    # --existing skips new object #30
    existing_rep = audit_database(new, old, rules=rules, existing_only=True)
    assert existing_rep["overview"]["objects_left_out_existing"] == 1
    assert existing_rep["builtins"]["duplicates"]["group_count"] == 0

    # --since filters by Last-modified UTC date
    since_rep = audit_database(new, old, rules=rules, since=dt.date(2026, 11, 25))
    assert since_rep["overview"]["objects_matched"] == 1
    assert since_rep["builtins"]["security"]["count"] == 1

    # Text formatter produces readable output
    text = format_audit_text(rep, limit=5)
    assert "[events/core-boundary]" in text and "$optional_mod" in text


def test_validate_rules_rejects_invalid_specs():
    with pytest.raises(AuditRuleError, match="unsupported rules version"):
        validate_rules({"version": 99})
    with pytest.raises(AuditRuleError, match="reserved rule id"):
        validate_rules({"version": 1, "line_rules": [{"id": "catches", "match": "x"}]})
    with pytest.raises(AuditRuleError, match="invalid regex"):
        validate_rules({"version": 1, "line_rules": [{"id": "bad", "match": "["}]})


def test_cli_audit_snapshot_json_and_rules(tmp_path: Path):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps({
        "version": 1,
        "name": "smoke",
        "line_rules": [{"id": "notify-metric", "mode": "metric", "scope": "all", "match": r"notify\("}],
    }), encoding="utf-8")

    r = CliRunner().invoke(
        moodb, ["--db", str(TOASTCORE), "--no-cache", "audit", "--rules", str(rules_file), "--format", "json"]
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["mode"] == "snapshot"
    assert data["rules_name"] == "smoke"
    assert data["rules"][0]["id"] == "notify-metric"
