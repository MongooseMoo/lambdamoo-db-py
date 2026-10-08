import json
from pathlib import Path

from click.testing import CliRunner

from lambdamoo_db.cli import moodb


FIXTURE = Path(__file__).parent / 'fixtures' / 'TypedMapKeys.db'


def test_self_compare_and_machine_footer():
    result = CliRunner().invoke(moodb, ['--db', str(FIXTURE), 'diff', str(FIXTURE), '--format', 'jsonl'])
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.output.splitlines()]
    assert records[0]['record'] == 'header'
    assert records[-1]['record'] == 'footer'
    assert records[-1]['status'] == 'equal'


def test_changed_status_survives_kind_filter_and_zero_display(tmp_path):
    new = tmp_path / 'new.db'
    new.write_bytes(FIXTURE.read_bytes().replace(b'map_test', b'map_best'))
    result = CliRunner().invoke(moodb, ['--db', str(new), 'diff', str(FIXTURE), '--kind', 'removed', '--max-events', '0'])
    assert result.exit_code == 1, result.output
    assert 'different' in result.output


def test_invalid_redaction_stop_and_format_options_fail_before_load(monkeypatch, tmp_path):
    import lambdamoo_db.diff_sources as sources
    monkeypatch.setattr(sources, 'freeze_source', lambda *_: (_ for _ in ()).throw(AssertionError('must not load')))
    rules = tmp_path / 'rules.json'
    rules.write_text('{"schema_version":1,"rules":[]}', encoding='utf-8')
    args = ['--db', 'absent', 'diff', 'absent']
    for options in (['--stop-after','1','--redact',str(rules)], ['--format','text','--output-dir',str(tmp_path/'report')]):
        result = CliRunner().invoke(moodb, args + options)
        assert result.exit_code == 2, result.output
        assert not isinstance(result.exception, AssertionError)


def test_self_contained_commands_ignore_invalid_moodb_env(tmp_path):
    result = CliRunner().invoke(moodb, ['diff-read',str(tmp_path/'missing')], env={'MOODB':'absent.db'})
    assert result.exit_code == 2
    assert 'absent.db' not in result.output
    result = CliRunner().invoke(moodb, ['--db','absent.db','diff-read',str(tmp_path/'missing')])
    assert result.exit_code == 2
    assert '--db' in result.output
    result = CliRunner().invoke(moodb, ['diff-read',str(tmp_path/'missing')], env={'MOODB':str(tmp_path)})
    assert result.exit_code == 2
    assert 'Invalid value for' not in result.output


def test_nonobject_manifest_is_safe_error(tmp_path):
    (tmp_path/'manifest.json').write_text('[]')
    result = CliRunner().invoke(moodb,['diff-read',str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert 'Error:' in result.output


def test_load_failure_jsonl_has_error_footer(tmp_path):
    result = CliRunner().invoke(moodb, ['--db',str(tmp_path/'missing'),'diff',str(FIXTURE),'--format','jsonl'])
    assert result.exit_code == 2
    records = [json.loads(line) for line in result.output.splitlines()]
    assert records[-1]['record'] == 'footer'
    assert records[-1]['status'] == 'error'


def test_missing_object_lookup_exits_two():
    result = CliRunner().invoke(moodb, ['--db',str(FIXTURE),'diff',str(FIXTURE),'-o','#999999'])
    assert result.exit_code == 2


def test_bundle_cli_drilldown(tmp_path):
    new = tmp_path / 'new.db'
    new.write_bytes(FIXTURE.read_bytes().replace(b'map_test', b'map_best'))
    output = tmp_path / 'report'
    result = CliRunner().invoke(moodb, ['--db',str(new),'diff',str(FIXTURE),'--output-dir',str(output)])
    assert result.exit_code == 1, result.output
    result = CliRunner().invoke(moodb, ['diff-read',str(output),'--object','#0'])
    assert result.exit_code == 0, result.output
    assert isinstance(json.loads(result.output), (dict,list))


def test_stream_encoding_failure_emits_exactly_one_footer(monkeypatch):
    from lambdamoo_db.diff_report import ReportError
    def broken(_):
        yield '{"record":"header","schema_version":1}\n'
        yield '{"record":"footer","status":"error","scan_complete":false}\n'
        raise ReportError('cannot encode report evidence')
    monkeypatch.setattr('lambdamoo_db.diff_report.iter_jsonl',broken)
    result = CliRunner().invoke(moodb,['--db',str(FIXTURE),'diff',str(FIXTURE),'--format','jsonl'])
    assert result.exit_code == 2
    records = [json.loads(line) for line in result.output.splitlines()]
    assert [row['record'] for row in records] == ['header','footer']
