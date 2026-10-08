"""Queued (forked) task headers.

ToastStunt's write_forked_task (src/tasks.cc) writes each queued task as
``0 <first lineno> <start time> <task id>``. The reader must put the start
time in ``st`` and the task id in ``id``, and the writer must emit them back
in the same positions.
"""
from io import StringIO
from pathlib import Path

import pytest

from lambdamoo_db.reader import load
from lambdamoo_db.writer import Writer

PROJECT_ROOT = Path(__file__).parent.parent

# (database, expected task id, expected start time), from each file's
# "1 queued tasks" header line.
QUEUED = [
    (PROJECT_ROOT / "toastcore.db", 419331400, 1671264001),
    (PROJECT_ROOT / "toast2.db", 145138543, 1675152001),
]


@pytest.mark.parametrize("path, task_id, start_time", QUEUED, ids=lambda v: getattr(v, "name", None))
def test_queued_task_id_and_start_time(path, task_id, start_time):
    db = load(str(path))
    assert len(db.queuedTasks) == 1
    task = db.queuedTasks[0]
    assert task.id == task_id
    assert task.st == start_time
    assert task.firstLineno == 1


@pytest.mark.parametrize("path, task_id, start_time", QUEUED, ids=lambda v: getattr(v, "name", None))
def test_queued_task_header_written_in_toaststunt_order(path, task_id, start_time):
    db = load(str(path))
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    lines = output.getvalue().splitlines()
    header = lines[lines.index("1 queued tasks") + 1]
    assert header == f"0 1 {start_time} {task_id}"
