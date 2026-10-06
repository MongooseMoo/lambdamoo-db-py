from io import StringIO
from pathlib import Path

import pytest

from lambdamoo_db.reader import load
from lambdamoo_db.writer import Writer

FIXTURE = Path(__file__).parent / "fixtures" / "Suspended.db"
# The last activation of the fixture's suspended task: pc line, then the built-in's name.
SUSPENDED_IN_EVAL = b"3 2 1\neval\n"


@pytest.mark.parametrize(
    "name, data",
    [
        ("move", ["bf_move data: what = 129079, where = 8608, position = 0"]),
        ("create", ["bf_create data: oid = 77"]),
        ("recreate", ["bf_create data: oid = 77"]),
        ("recycle", ["bf_recycle data: oid = 77, cont = 0"]),
        ("call_function", ["bf_call_function data: fname = move", "bf_move data: what = 1, where = 2, position = 0"]),
        ("call_function", ["bf_call_function data: fname = eval"]),
    ],
)
def test_task_suspended_in_a_builtin_with_saved_state_round_trips(tmp_path, name, data):
    original = FIXTURE.read_bytes()
    assert original.count(SUSPENDED_IN_EVAL) == 1
    lines = "".join(line + "\n" for line in [name, *data]).encode("latin-1")
    dump = original.replace(SUSPENDED_IN_EVAL, b"3 2 1\n" + lines)
    path = tmp_path / "suspended.db"
    path.write_bytes(dump)

    db = load(str(path))

    activation = db.suspendedTasks[0].vm.stack[-1]
    assert activation.bi_func_name == name
    assert activation.bi_func_data == data
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    assert output.getvalue().encode("latin-1") == dump
