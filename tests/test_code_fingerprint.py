import hashlib
import json

from click.testing import CliRunner

import lambdamoo_db.cli as cli
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum, Verb
from lambdamoo_db.inspection import code_fingerprint


def verb(lines):
    result = Verb("private_alias", ObjNum(2), 173, -1, 1)
    result.code = lines
    return result


def test_hash_preserves_source_bytes_and_distinguishes_absent_empty():
    assert code_fingerprint(verb(["caf\xe9", ""]))["sha256"] == hashlib.sha256(b"caf\xe9\n\n").hexdigest()
    absent = code_fingerprint(verb(None))
    empty = code_fingerprint(verb([]))
    assert absent["program_present"] is False
    assert absent["line_count"] is absent["sha256"] is None
    assert empty["program_present"] is True
    assert empty["line_count"] == 0
    assert empty["sha256"] == hashlib.sha256(b"").hexdigest()
    assert code_fingerprint(verb(["a", "b"]))["sha256"] != code_fingerprint(verb(["ab"]))["sha256"]


def test_stamp_date_only_and_unrecognized_dates():
    stamp = '"Last modified Tue Oct 13 04:00:00 2026 UTC by PRIVATE_SENTINEL.";'
    result = code_fingerprint(verb(["PRIVATE_SENTINEL", stamp]))
    assert result["modified_utc"] == "2026-10-13T04:00:00+00:00"
    assert result["footer_sha256"] == hashlib.sha256(stamp.encode("latin-1")).hexdigest()
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    for text in ['"Last modified PRIVATE_SENTINEL";', '"Last modified Tue Oct 99 04:00:00 2026 UTC by PRIVATE_SENTINEL.";', stamp.replace("UTC", "CET")]:
        assert code_fingerprint(verb([text]))["modified_utc"] is None


def test_cli_fingerprint_suppresses_private_payload_and_retains_default(monkeypatch):
    db = MooDatabase()
    parent = MooObject(id=1, name="private_object", flags=0, owner=ObjNum(2), location=ObjNum(-1))
    parent.verbs = [verb(["PRIVATE_SENTINEL"]), verb([])]
    child = MooObject(id=2, name="child", flags=0, owner=ObjNum(2), location=ObjNum(-1))
    child.parents = [ObjNum(1)]
    db.objects = {1: parent, 2: child}
    monkeypatch.setattr(cli, "_db", lambda ctx: (db, {}))
    runner = CliRunner()
    result = runner.invoke(cli.moodb, ["code", "--fingerprint", "#2:private_alias", "#1:1"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [(r["object"], r["index"], r["native_slot"]) for r in rows] == [(1, 0, 1), (1, 1, 2)]
    assert "PRIVATE_SENTINEL" not in result.output
    assert "private_alias" not in result.output
    assert runner.invoke(cli.moodb, ["code", "--fingerprint", "#1"]).exit_code == 0
    ordinary = runner.invoke(cli.moodb, ["code", "#1:0"])
    assert ordinary.exit_code == 0
    assert ordinary.output == '@program #1 "private_object":[0] "private_alias"  this none this  rxd  owner #2\nPRIVATE_SENTINEL\n.\n'
    invalid = runner.invoke(cli.moodb, ["code", "--fingerprint", "-n", "#1:0"])
    assert invalid.exit_code == 2
    missing = runner.invoke(cli.moodb, ["code", "--fingerprint", "#1:missing"])
    assert missing.exit_code == 1
    assert "PRIVATE_SENTINEL" not in missing.output
