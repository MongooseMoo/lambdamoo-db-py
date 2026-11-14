"""Behavioral contracts for `moodb builtins`: which builtin functions verb code calls."""
import json

import pytest
from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum, Verb
from lambdamoo_db.inspection import builtin_calls, builtin_calls_in_code, builtin_wrappers


def calls(*lines, wrappers=()):
    return list(builtin_calls_in_code(list(lines), wrappers))


def test_identifier_before_paren_is_a_call_and_names_fold_case():
    assert calls('return length(x) + LENGTH(y);', 'return time();') == [
        ('length', 1, '1', 'direct'), ('length', 1, '1', 'direct'), ('time', 2, '0', 'direct')]


def test_verb_calls_properties_strings_and_keywords_are_not_calls():
    assert calls('this:length(x);', '$length(x);', 'return x.length;', 'return "valid(x, y)";',
                 'if (x)', 'endif', 'return (1);') == []


def test_labels_of_while_fork_and_except_are_not_calls():
    assert calls('while loop (length(l))', 'endwhile', 'fork t (0)', 'endfork',
                 'try', 'x = 1;', 'except e (E_TYPE)', 'endtry') == [('length', 1, '1', 'direct')]


def test_arguments_are_counted_at_top_level_with_splices_marked():
    assert calls('valid(who, {1, 2}, [1 -> 2], f(a, b), "x,y");') == [
        ('f', 1, '2', 'direct'), ('valid', 1, '5', 'direct')]
    assert calls('{a, @b} = args;', 'return tostr(@b);', 'return tostr(a, @b);') == [
        ('tostr', 2, '1@', 'direct'), ('tostr', 3, '2@', 'direct')]


def test_ranges_and_catch_expressions_do_not_hide_calls():
    assert calls('return l[1..length(l)];', "return `valid(x) ! ANY => 0';") == [
        ('length', 1, '1', 'direct'), ('valid', 2, '1', 'direct')]


def test_call_function_with_a_literal_name_counts_the_named_builtin():
    assert calls('call_function("Parse_AST", s, 1);', 'call_function(name, s);', 'call_function("a" + b, s);') == [
        ('call_function', 1, '3', 'direct'), ('parse_ast', 1, '2', 'call_function'),
        ('call_function', 2, '2', 'direct'), ('call_function', 3, '2', 'direct')]


def test_bf_wrapper_verb_calls_count_only_for_known_wrappers():
    assert calls('$wrappers:bf_valid(o);', '#0:BF_VALID(o, i);', 'x:bf_unknown(1);', wrappers={'valid'}) == [
        ('valid', 1, '1', 'bf_wrapper'), ('valid', 2, '2', 'bf_wrapper')]


@pytest.fixture
def builtin_db():
    db = MooDatabase()
    # #0 inherits from #5, which holds the wrappers; #10 holds ordinary code.
    db.objects[0] = MooObject(0, "system", 0, ObjNum(2), ObjNum(-1), parents=[ObjNum(5)])
    db.objects[5] = MooObject(5, "layer", 0, ObjNum(2), ObjNum(-1))
    db.objects[10] = MooObject(10, "thing", 0, ObjNum(2), ObjNum(-1))

    def add(number, name, lines):
        verb = Verb(name, ObjNum(2), 13, -1, number)
        verb.code = lines
        db.objects[number].verbs.append(verb)

    add(5, "bf_valid", ['return valid(@args);'])
    add(10, "check", ['if (valid(who, inc))', 'return #0:bf_valid(who);', 'endif'])
    add(10, "parse", ['return call_function("parse_ast", src);'])
    return db


def test_wrappers_are_found_through_the_ancestry_of_0(builtin_db):
    assert builtin_wrappers(builtin_db) == {'valid': (5, 0)}
    found = {(c.name, c.obj, c.index, c.line, c.args, c.via, c.in_wrapper) for c in builtin_calls(builtin_db)}
    assert found == {
        ('valid', 5, 0, 1, '1@', 'direct', True),
        ('valid', 10, 0, 1, '2', 'direct', False),
        ('valid', 10, 0, 2, '1', 'bf_wrapper', False),
        ('call_function', 10, 1, 1, '2', 'direct', False),
        ('parse_ast', 10, 1, 1, '1', 'call_function', False),
    }
    del builtin_db.objects[0]
    assert builtin_wrappers(builtin_db) == {}


@pytest.fixture
def invoke(builtin_db, monkeypatch, tmp_path):
    import lambdamoo_db.cli as cli
    path = tmp_path / 'builtins.db'
    path.touch()
    monkeypatch.setattr(cli, 'open_indexed', lambda *_: builtin_db)
    monkeypatch.setattr(cli, 'dollar_names', lambda _: {})
    return lambda *args: CliRunner().invoke(moodb, ['--db', str(path), *args])


def test_cli_json_counts_sites_verbs_objects_arguments_and_routes(invoke):
    result = invoke('builtins', '--format', 'json')
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data['called'] == 3 and data['sites'] == 5
    valid = next(r for r in data['builtins'] if r['name'] == 'valid')
    assert valid == {'name': 'valid', 'sites': 3, 'verbs': 2, 'objects': 2,
                     'arg_counts': {'1': 1, '1@': 1, '2': 1}, 'via': {'bf_wrapper': 1, 'direct': 2}}
    assert [r['name'] for r in data['builtins']] == ['call_function', 'parse_ast', 'valid']


def test_cli_against_reports_only_names_missing_from_the_list(invoke, tmp_path):
    known = tmp_path / 'known.txt'
    known.write_text('VALID\n\ncall_function\n', encoding='utf-8')
    result = invoke('builtins', '--against', str(known), '--sites', '--format', 'json')
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert (data['known'], data['called'], data['missing']) == (2, 3, 1)
    assert data['builtins'] == [{'name': 'parse_ast', 'sites': 1, 'verbs': 1, 'objects': 1,
                                 'arg_counts': {'1': 1}, 'via': {'call_function': 1},
                                 'calls': [{'object': 10, 'index': 1, 'verb': 'parse', 'line': 1,
                                            'args': '1', 'via': 'call_function', 'in_wrapper': False}]}]
    text = invoke('builtins', '--against', str(known), '--sites')
    assert text.exit_code == 0, text.output
    assert text.output.splitlines() == [
        'parse_ast  1 sites in 1 verbs on 1 objects  args 1:1  via call_function:1',
        '  #10:[1] parse:1  args 1  via call_function',
        f'1 of 3 called builtins are not in {known}',
    ]


def test_cli_names_limit_the_report_and_text_marks_wrapper_sites(invoke):
    result = invoke('builtins', 'VALID', '--sites')
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        'valid  3 sites in 2 verbs on 2 objects  args 1:1 1@:1 2:1  via bf_wrapper:1 direct:2',
        '  #5:[0] bf_valid:1  args 1@  in bf_ wrapper',
        '  #10:[0] check:1  args 2',
        '  #10:[0] check:2  args 1  via bf_wrapper',
        '1 builtins called at 3 sites',
    ]
