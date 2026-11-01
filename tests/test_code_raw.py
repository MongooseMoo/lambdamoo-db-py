from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum, Verb


def test_raw_source_is_one_complete_program_without_headers(monkeypatch):
    import lambdamoo_db.cli as cli
    db = MooDatabase()
    obj = MooObject(1, 'fixture', 0, ObjNum(2), ObjNum(-1))
    source = ['"comment";', '', 'return "quoted \\\"";', '"Last modified stamp";']
    v = Verb('run', ObjNum(2), 13, -1, 1)
    v.code = source
    obj.verbs = [v]
    db.objects[1] = obj
    monkeypatch.setattr(cli, '_db', lambda _: (db, {}))
    runner = CliRunner()
    result = runner.invoke(moodb, ['code', '--raw', '#1:run'])
    assert result.exit_code == 0, result.output
    assert result.output == '\n'.join(source) + '\n'
    for arguments in (['#1'], ['#1:run', '#1:run'], ['-n', '#1:run'], ['--fingerprint', '#1:run']):
        result = runner.invoke(moodb, ['code', '--raw', *arguments])
        assert result.exit_code == 2, result.output
    v.code = []
    assert runner.invoke(moodb, ['code', '--raw', '#1:run']).output == ''
    v.code = None
    missing = runner.invoke(moodb, ['code', '--raw', '#1:run'])
    assert missing.exit_code == 1 and 'no program' in missing.output
