import json
from pathlib import Path

import pytest
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


def test_invalid_redaction_and_stop_options_fail_before_load(monkeypatch, tmp_path):
    import lambdamoo_db.diff_sources as sources
    monkeypatch.setattr(sources, 'freeze_source', lambda *_: (_ for _ in ()).throw(AssertionError('must not load')))
    rules = tmp_path / 'rules.json'
    rules.write_text('{"schema_version":1,"rules":[]}', encoding='utf-8')
    args = ['--db', 'absent', 'diff', 'absent']
    for options in (['--stop-after','1','--redact',str(rules)], ['--redact-strict']):
        result = CliRunner().invoke(moodb, args + options)
        assert result.exit_code == 2, result.output
        assert not isinstance(result.exception, AssertionError)


def _changed_fixture(tmp_path):
    new = tmp_path / 'new.db'
    new.write_bytes(FIXTURE.read_bytes().replace(b'map_test', b'map_best'))
    return new


def _bundle_files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


@pytest.mark.parametrize('extra', [[], ['--max-events', '0'], ['--kind', 'removed'], ['--view', 'code'], 'redact'])
@pytest.mark.parametrize('output_format', ['text', 'jsonl'])
def test_format_and_output_dir_combine_without_changing_either(tmp_path, output_format, extra):
    if extra == 'redact':
        rules = tmp_path / 'rules.json'
        rules.write_text('{"schema_version":1,"rules":[]}', encoding='utf-8')
        extra = ['--redact', str(rules)]
    base = ['--db', str(_changed_fixture(tmp_path)), 'diff', str(FIXTURE), *extra]
    stream_only = CliRunner().invoke(moodb, base + ['--format', output_format])
    bundle_only = CliRunner().invoke(moodb, base + ['--output-dir', str(tmp_path / 'alone')])
    assert stream_only.exit_code == bundle_only.exit_code, (stream_only.output, bundle_only.output)
    both = CliRunner().invoke(moodb, base + ['--format', output_format, '--output-dir', str(tmp_path / 'both')])
    assert both.exit_code == stream_only.exit_code, both.output
    assert both.stdout == stream_only.stdout
    assert _bundle_files(tmp_path / 'both') == _bundle_files(tmp_path / 'alone')
    assert both.stderr == bundle_only.stdout.replace('alone', 'both')


def test_format_and_output_dir_share_one_comparison(monkeypatch, tmp_path):
    import lambdamoo_db.compare as compare
    calls = []
    original = compare.compare_snapshots
    monkeypatch.setattr(compare, 'compare_snapshots', lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs))
    result = CliRunner().invoke(moodb, ['--db', str(_changed_fixture(tmp_path)), 'diff', str(FIXTURE),
                                        '--format', 'text', '--output-dir', str(tmp_path / 'report')])
    assert result.exit_code == 1, result.output
    assert calls == [1]


def test_text_with_output_dir_keeps_its_default_limit_and_the_bundle_keeps_everything(tmp_path):
    output = tmp_path / 'report'
    result = CliRunner().invoke(moodb, ['--db', str(_changed_fixture(tmp_path)), 'diff', str(FIXTURE),
                                        '--format', 'text', '--output-dir', str(output)])
    assert result.exit_code == 1, result.output
    assert '"max_events":200' in result.stdout
    assert json.loads((output / 'manifest.json').read_bytes())['header']['options']['max_events'] is None


def test_existing_bundle_destination_fails_before_any_jsonl_evidence(tmp_path):
    output = tmp_path / 'report'
    output.mkdir()
    result = CliRunner().invoke(moodb, ['--db', str(_changed_fixture(tmp_path)), 'diff', str(FIXTURE),
                                        '--format', 'jsonl', '--output-dir', str(output)])
    assert result.exit_code == 2, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row['record'] for row in records] == ['header', 'footer']
    assert records[-1]['status'] == 'error'


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
