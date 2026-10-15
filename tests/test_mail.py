import json
from io import StringIO
from pathlib import Path

import pytest
from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import CLEAR, MooDatabase, MooObject, ObjNum, Property
from lambdamoo_db.inspection import LookupFailed
from lambdamoo_db.mail import read_mailbox, select_messages
from lambdamoo_db.reader import Reader


def mailbox(values, names=None):
    db = MooDatabase()
    obj = MooObject(10, "Changelog", 0, 0, -1, [])
    obj.properties = [Property(n, v, 0, 5) for n, v in zip(names or ["messages"], values)]
    obj.propdefs_count = len(obj.properties)
    db.objects[10] = obj
    return db, obj


def text(subject="A new room", body=None):
    return [1_000_000, "Builder (#2)", "*Changelog (#10)", subject,
            "Reply-to: *Builders", "X-Note: preserved", "", *(body if body is not None else ["Go north.", "", "Enjoy."])]


def test_flat_mail_preserves_sparse_numbers_headers_and_blank_lines():
    db, obj = mailbox([[[3, text()], [8, text("Fixed a door")], [25, text("A map")]]])
    messages = read_mailbox(db, obj)
    assert [m.number for m in messages] == [3, 8, 25]
    assert messages[0].headers == ("Reply-to: *Builders", "X-Note: preserved")
    assert messages[0].body == ("Go north.", "", "Enjoy.")
    assert [m.number for m in select_messages(messages, "4-25")] == [8, 25]
    assert [m.number for m in select_messages(messages, "3,20-25")] == [3, 25]
    assert [m.number for m in select_messages(messages, search="DOOR", tail=1)] == [8]
    assert select_messages(messages, search="absent") == []
    assert [m.number for m in select_messages(messages, tail=2)] == [8, 25]


def test_inherited_store():
    db, parent = mailbox([[[7, text()]]])
    child = MooObject(11, "Child", 0, 0, -1, [ObjNum(10)])
    child.properties = [Property("messages", CLEAR, 0, 5)]
    child.propdefs_count = 0
    db.objects[11] = child
    Reader(StringIO()).process_propnames(db, child)
    assert read_mailbox(db, child)[0].number == 7


def test_big_mail_reads_only_active_tree_and_reassembles_body():
    msg = text()
    db, obj = mailbox([
        [" root", 2, [3, msg[0]]],
        [1, [[" leaf", 2, [3, msg[0]]]]],
        [0, [[" body", 3, *msg[:6]], [0, 25, *msg[:4]]]],
        ["Go north.", "", "Enjoy."],
        [[99, text("Deleted")]],
    ], ["messages", " root", " leaf", " body", "messages_going"])
    messages = read_mailbox(db, obj)
    assert [m.number for m in messages] == [3, 25]
    assert messages[0].body == ("Go north.", "", "Enjoy.")
    assert messages[1].body == ()
    obj.properties[1].value = [1, [[" root", 2, []]]]
    with pytest.raises(LookupFailed, match="cyclic"):
        read_mailbox(db, obj)


@pytest.mark.parametrize("raw", [0, "secret", [[1]], [[1, [0, "a"]]], [[1, text()], [1, text()]], [[True, text()]]])
def test_malformed_mail_fails_without_printing_raw_values(raw):
    db, obj = mailbox([raw])
    with pytest.raises(LookupFailed) as raised:
        read_mailbox(db, obj)
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("sequence", ["0", "-1", "5-2", "1,", "all,1", "1-2-3", "2"])
def test_invalid_or_missing_number_selection(sequence):
    db, obj = mailbox([[[3, text()]]])
    with pytest.raises(LookupFailed):
        select_messages(read_mailbox(db, obj), sequence)


def test_empty_and_no_separator():
    db, obj = mailbox([[]])
    assert read_mailbox(db, obj) == []
    obj.properties[0].value = [[1, text()[:6]], [2, text(body=[])]]
    messages = read_mailbox(db, obj)
    assert messages[0].headers == ("Reply-to: *Builders", "X-Note: preserved")
    assert messages[0].body == messages[1].body == ()


def test_cli_read_headers_search_and_batch(monkeypatch, tmp_path):
    import lambdamoo_db.cli as cli

    db, obj = mailbox([[[3, text()], [8, text("Fixed a door")]]])
    # Populate the context through the normal loader.
    monkeypatch.setattr(cli, "open_indexed", lambda *_: db)
    monkeypatch.setattr(cli, "dollar_names", lambda *_: {})
    path = tmp_path / "world.db"
    path.touch()
    runner = CliRunner()
    prefix = ["--db", str(path)]
    result = runner.invoke(moodb, prefix + ["mail", "read", "10", "3", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["messages"][0]["body"] == ["Go north.", "", "Enjoy."]
    result = runner.invoke(moodb, prefix + ["mail", "headers", "10", "--search", "door", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [m["number"] for m in payload["messages"]] == [8]
    assert "body" not in payload["messages"][0]
    result = runner.invoke(moodb, prefix + ["batch", "mail read 10 3", "mail headers 10 --tail 1"])
    assert result.exit_code == 0, result.output
    assert "Go north.\n\nEnjoy." in result.output
    assert "[8]" in result.output
    result = runner.invoke(moodb, prefix + ["mail", "read", "10", "4"])
    assert result.exit_code == 1
    assert "No stored messages" in result.output


def test_real_core_flat_and_big_mailboxes():
    from lambdamoo_db.reader import load
    from lambdamoo_db.inspection import resolve_object

    db = load(str(Path(__file__).parent.parent / "toastcore.db"))
    assert read_mailbox(db, resolve_object(db, "$mail_recipient")) == []
    assert read_mailbox(db, resolve_object(db, "$big_mail_recipient")) == []
