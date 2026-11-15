"""`contents` and `children` can show each object's ancestors on its line."""

import pytest
from click.testing import CliRunner

from lambdamoo_db import cli
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum


def world():
    db = MooDatabase()
    db.objects[0] = MooObject(0, "system", 0, 0, -1)
    for number, name, parents in [(1, "room", []), (2, "coffee table", [3]), (3, "generic table", [4]),
                                  (4, "generic furniture", [])]:
        obj = MooObject(number, name, 0, 0, -1)
        obj.parents = [ObjNum(p) for p in parents]
        db.objects[number] = obj
    db.objects[1].contents = [ObjNum(2)]
    db.objects[1].children = [ObjNum(2)]
    return db


@pytest.mark.parametrize("relation", ["children", "contents"])
def test_ancestry_follows_each_object_nearest_parent_first(monkeypatch, tmp_path, relation):
    db = world()
    monkeypatch.setattr(cli, "open_indexed", lambda *_: db)
    dump = tmp_path / "world.db"
    dump.touch()
    runner = CliRunner()
    plain = runner.invoke(cli.moodb, ["--db", str(dump), relation, "#1"])
    with_ancestry = runner.invoke(cli.moodb, ["--db", str(dump), relation, "#1", "--ancestry"])
    assert plain.exit_code == with_ancestry.exit_code == 0, (plain.output, with_ancestry.output)
    assert plain.output == '#2 "coffee table"\n'
    assert with_ancestry.output == '#2 "coffee table"  < #3 "generic table"  < #4 "generic furniture"\n'
