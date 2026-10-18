"""Behavioral contracts for repeatable, whole-verb source audits."""
import json
import re

import pytest
from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum, Verb
from lambdamoo_db.inspection import grep_verbs


@pytest.fixture
def source_db():
    db = MooDatabase()
    for number, flags in ((2, 4), (3, 2), (10, 0), (11, 0)):
        db.objects[number] = MooObject(number, "fixture", flags, ObjNum(2), ObjNum(-1))

    def add(number, name, lines, owner=2, perms=13):
        verb = Verb(name, ObjNum(owner), perms, -1, number)
        verb.code = lines
        db.objects[number].verbs.append(verb)

    add(10, "inline", ['"owner.wizard is documentation";', 'if (actor.wizard || actor == target.owner)', 'return 1;', 'endif'])
    add(10, "central", ['if (actor.wizard)', 'return $perm_utils:controls(actor, target);', 'endif'])
    add(10, "comment_guard", ['"$perm_utils:controls(actor, target)";', 'return actor.wizard || actor == target.owner;'])
    add(10, "string_only", ['return "actor.wizard";'])
    add(11, "ordinary", ['return actor.wizard;'], owner=3)
    add(11, "command", ['return actor.wizard;'], perms=9)
    return db


def test_whole_verb_filters_are_conjunctive_and_preserve_line_evidence(source_db):
    hits = list(grep_verbs(source_db, re.compile(r'\.wizard'),
                           require=[re.compile(r'\.owner')],
                           exclude=[re.compile(r'\$perm_utils\s*:')], code_only=True))
    assert [(h.obj.id, h.index, h.lineno, h.line) for h in hits] == [
        (10, 0, 2, 'if (actor.wizard || actor == target.owner)'),
        (10, 2, 2, 'return actor.wizard || actor == target.owner;'),
    ]
    # Raw mode intentionally sees comment text, including the excluded guard.
    raw = list(grep_verbs(source_db, re.compile(r'\.wizard'),
                          require=[re.compile(r'\.owner')], exclude=[re.compile(r'\$perm_utils\s*:')]))
    assert {(h.obj.id, h.index) for h in raw} == {(10, 0)}


def test_metadata_filters_do_not_infer_authority_from_object_owner(source_db):
    hits = list(grep_verbs(source_db, re.compile(r'\.wizard'), code_only=True,
                           wizard_owned=True, executable=True))
    assert {(h.obj.id, h.index) for h in hits} == {(10, 0), (10, 1), (10, 2)}
    ordinary = list(grep_verbs(source_db, re.compile(r'\.wizard'), owners={3}))
    assert [(h.obj.id, h.index) for h in ordinary] == [(11, 0)]
    source_db.objects.pop(2)
    assert list(grep_verbs(source_db, re.compile('.'), wizard_owned=True)) == []


def test_string_mask_handles_escapes_multiline_and_token_boundaries(source_db):
    verb = source_db.objects[10].verbs[0]
    verb.code = [r'"escaped \\"; actor.wizard;', '"first', 'actor.wizard"; return actor.owner;',
                 r'return "\"actor.wizard";', 'return foo"ignored"bar;', 'return actor.wizard;']
    hits = list(grep_verbs(source_db, re.compile(r'\.wizard|foobar'),
                           objs=[source_db.objects[10]], code_only=True))
    assert [(h.index, h.lineno) for h in hits if h.index == 0] == [(0, 1), (0, 6)]


@pytest.fixture
def invoke(source_db, monkeypatch, tmp_path):
    import lambdamoo_db.cli as cli
    path = tmp_path / 'source.db'
    path.touch()
    monkeypatch.setattr(cli, 'open_indexed', lambda *_: source_db)
    monkeypatch.setattr(cli, 'dollar_names', lambda _: {})
    return lambda *args: CliRunner().invoke(moodb, ['--db', str(path), *args])


def test_cli_summary_counts_all_matches_without_source_or_names(invoke):
    result = invoke('grep', r'\.wizard', '--code-only', '--summary', '--format', 'json')
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data == {
        'matched_lines': 5, 'matched_verbs': 5, 'matched_objects': 2,
        'objects': [{'object': 10, 'verbs': 3, 'lines': 3}, {'object': 11, 'verbs': 2, 'lines': 2}],
        'owners': [{'owner': 2, 'verbs': 4, 'lines': 4}, {'owner': 3, 'verbs': 1, 'lines': 1}],
    }
    assert 'actor' not in result.output and 'inline' not in result.output


def test_cli_json_page_keeps_global_counts_and_native_metadata(invoke):
    result = invoke('grep', r'\.wizard', '--code-only', '--wizard-owned', '--executable',
                    '--without', r'\$perm_utils\s*:', '--format', 'json', '--limit', '1', '--offset', '1')
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert (data['matched_verbs'], data['matched_lines'], data['matched_objects']) == (2, 2, 1)
    assert data['truncated'] is True and data['next_offset'] is None
    assert data['verbs'] == [{'object': 10, 'index': 2, 'names': 'comment_guard', 'owner': 2,
                              'perms': 'rxd', 'args': 'none none none',
                              'lines': [{'line': 2, 'text': 'return actor.wizard || actor == target.owner;'}]}]
    metadata = invoke('grep', r'\.wizard', '--code-only', '-l', '--format', 'json', '--limit', '1')
    assert 'lines' not in json.loads(metadata.output)['verbs'][0]
    assert json.loads(metadata.output)['next_offset'] == 1


@pytest.mark.parametrize('option', ['--require', '--without'])
def test_invalid_filter_regex_fails_before_database_load(invoke, option, monkeypatch):
    import lambdamoo_db.cli as cli
    monkeypatch.setattr(cli, '_db', lambda _: pytest.fail('invalid query loaded database'))
    result = invoke('grep', 'x', option, '(')
    assert result.exit_code == 2 and 'Invalid value' in result.output


def test_cli_empty_summary_and_text_page(invoke):
    empty = invoke('grep', 'absent', '--summary', '--format', 'json')
    assert json.loads(empty.output) == {'matched_lines': 0, 'matched_verbs': 0, 'matched_objects': 0,
                                       'objects': [], 'owners': []}
    page = invoke('grep', r'\.wizard', '--code-only', '-l', '--limit', '1')
    assert page.exit_code == 0 and page.output.count('"inline"') == 1
    assert '5 matching lines in 5 verbs on 2 objects' in page.output
