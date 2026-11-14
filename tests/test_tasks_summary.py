"""``moodb tasks --summary``: tasks counted by kind and by the verb they are in.

The fixture is toastcore.db (one queued task, in #79:schedule_measurement_task)
with suspended and interrupted tasks added and written back as a dump.
"""
from copy import deepcopy
from pathlib import Path

from click.testing import CliRunner

import lambdamoo_db.cli as cli
from lambdamoo_db.database import VM, InterruptedTask, SuspendedTask
from lambdamoo_db.inspection import TaskInfo, task_summary
from lambdamoo_db.reader import load
from lambdamoo_db.writer import Writer

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"


def frame(base, vloc, verb, this):
    a = deepcopy(base)
    a.vloc, a.verb, a.verbname, a.this = vloc, verb, verb, this  # plain ints, as the reader gives them
    return a


def dump_with_tasks(tmp_path):
    db = load(str(TOASTCORE))
    base = db.queuedTasks[0].activation
    stacks = [  # (kind, task id, frames outermost first)
        ("suspended", 501, [frame(base, 10, "outer", 10), frame(base, 20, "wait", 20)]),
        ("suspended", 502, [frame(base, 20, "wait", 30)]),
        ("suspended", 503, [frame(base, 20, "wait", 20)]),
        ("interrupted", 601, [frame(base, 30, "loop", 30)]),
    ]
    for kind, task_id, frames in stacks:
        if kind == "suspended":
            task = SuspendedTask(1, task_id, 1700000000)
            task.value = 0
            db.suspendedTasks.append(task)
        else:
            task = InterruptedTask(task_id, "interrupted")
            db.interruptedTasks.append(task)
        task.vm = VM({}, frames, top=len(frames) - 1)  # top indexes the innermost frame
    path = tmp_path / "tasks.db"
    with open(path, "w", encoding="latin-1", newline="") as out:
        Writer(db=db, output_file=out).writeDatabase()
    return path


def run(path, *args):
    result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(path), "tasks", *args])
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def test_summary_counts_kinds_and_innermost_verbs(tmp_path):
    lines = run(dump_with_tasks(tmp_path), "--summary")
    assert lines[0] == "5 tasks: 1 queued, 3 suspended, 1 interrupted"
    assert [line.split() for line in lines[1:]] == [
        ["3", "#20:wait"],  # an outer frame does not count; the innermost does
        ["1", "#30:loop"],
        ["1", "#79:schedule_measurement_task"],
    ]


def test_summary_counts_only_the_tasks_this_selects(tmp_path):
    lines = run(dump_with_tasks(tmp_path), "--summary", "--this", "#20")
    assert lines == ["2 tasks: 0 queued, 2 suspended, 0 interrupted", "     2  #20:wait"]


def test_summary_of_no_tasks_shows_every_kind_at_zero():
    counts = task_summary([])
    assert counts.kinds == {"queued": 0, "suspended": 0, "interrupted": 0}
    assert counts.verbs == []


def test_summary_drops_interrupted_status_and_names_frameless_tasks():
    counts = task_summary([TaskInfo("interrupted (shutdown)", 1, None, []), TaskInfo("queued", 2, 0, [])])
    assert counts.kinds == {"queued": 1, "suspended": 0, "interrupted": 1}
    assert counts.verbs == [("(no frames)", 2)]
