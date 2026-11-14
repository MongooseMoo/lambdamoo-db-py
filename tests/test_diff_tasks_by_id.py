"""moodb diff pairs saved tasks by id, splits VM-private state from what a task
is, and names differences that are only a trailing CR or name whitespace."""
import json
from copy import deepcopy
from io import StringIO
from pathlib import Path

from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.compare import compare_snapshots
from lambdamoo_db.diff_types import ReportOptions
from lambdamoo_db.reader import load, load_bytes
from lambdamoo_db.writer import Writer
from test_full_diff_loading import database_bytes, object_record

SUSPENDED = Path(__file__).parent / "fixtures" / "Suspended.db"
CHECKPOINT = ReportOptions(view="checkpoint")


def two_tasks():
    """The synthetic fixture's suspended task plus a second one with another id."""
    db = load(str(SUSPENDED))
    second = deepcopy(db.suspendedTasks[0])
    second.id = db.suspendedTasks[0].id + 1
    db.suspendedTasks.append(second)
    return db


def runtime(result):
    return [e for e in result.events if e["section"] == "runtime"]


def task_step(task_id):
    return {"type": "task", "id": str(task_id)}


def test_reordered_tasks_pair_by_id_and_only_the_order_is_reported():
    old = two_tasks()
    new = deepcopy(old)
    new.suspendedTasks.reverse()
    result = compare_snapshots(old, new, CHECKPOINT)
    assert runtime(result) == []
    [order] = result.events
    assert (order["section"], order["kind"], order["details"]) == ("serialization", "order_changed", {"subtype": "task_order"})
    assert result.footer["runtime_changes"] == {"task": 0, "vm_private": 0, "other": 0}


def test_a_task_on_one_side_is_one_event_at_its_id():
    old = two_tasks()
    new = deepcopy(old)
    gone = new.suspendedTasks.pop(0)
    result = compare_snapshots(old, new, CHECKPOINT)
    [event] = runtime(result)
    assert result.footer["task_pairing"]["suspendedTasks"] == {"matched": 1, "old_only": 1, "new_only": 0}
    assert event["kind"] == "removed"
    assert event["old_path"] == [{"type": "field", "name": "suspendedTasks"}, task_step(gone.id)]
    assert event["details"] == {"part": "task"}
    [added] = runtime(compare_snapshots(new, old, CHECKPOINT))
    assert added["kind"] == "added" and added["new_path"][1] == task_step(gone.id)


def test_vm_private_frame_fields_are_reported_apart_from_what_the_task_is():
    old = two_tasks()
    new = deepcopy(old)
    task = new.suspendedTasks[1]
    frame = task.vm.stack[0]
    frame.pc += 5
    frame.stack = frame.stack + [7]
    frame.temp_end = ["engine metadata"]
    task.vm.funcId = 1
    frame.programmer = frame.programmer + 1
    frame.rtEnv = {**frame.rtEnv, "added": 1}
    result = compare_snapshots(old, new, CHECKPOINT)
    parts = {}
    for event in runtime(result):
        path = event["new_path"]
        assert path[:2] == [{"type": "field", "name": "suspendedTasks"}, task_step(task.id)]
        field = next(step["name"] for step in reversed(path) if step["type"] == "field")
        parts[field] = event["details"]["part"]
    assert parts == {"pc": "vm_private", "stack": "vm_private", "temp_end": "vm_private", "funcId": "vm_private",
                     "programmer": "task", "rtEnv": "task"}
    assert result.footer["runtime_changes"] == {"task": 2, "vm_private": 4, "other": 0}


def test_only_vm_private_changes_leave_no_task_changes():
    old = two_tasks()
    new = deepcopy(old)
    for task in new.suspendedTasks:
        for frame in task.vm.stack:
            frame.pc, frame.error, frame.language_version = frame.pc + 1, 3, 99
    result = compare_snapshots(old, new, CHECKPOINT)
    assert result.footer["runtime_changes"]["task"] == 0
    assert result.footer["runtime_changes"]["vm_private"] > 0


def test_lf_dump_keeps_a_trailing_carriage_return_and_writes_it_back():
    data = database_bytes([object_record(name="room\r")])
    db = load_bytes(data)
    assert db.line_ending == "\n" and db.objects[0].name == "room\r"
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    assert output.getvalue().encode("latin-1") == data
    crlf = database_bytes([object_record(name="room\r")]).replace(b"\n", b"\r\n")
    assert load_bytes(crlf).objects[0].name == "room\r"


def test_a_trailing_carriage_return_is_named_on_any_string():
    old = load_bytes(database_bytes([object_record(name="room\r")]))
    new = load_bytes(database_bytes([object_record(name="room")]))
    [event] = compare_snapshots(old, new).events
    assert event["details"]["whitespace"] == "trailing carriage return"
    assert (event["old"]["value"], event["new"]["value"]) == ("room\r", "room")
    tasks_old = two_tasks()
    tasks_new = deepcopy(tasks_old)
    frame = tasks_new.suspendedTasks[0].vm.stack[0]
    frame.verb = frame.verb + "\r"
    [event] = runtime(compare_snapshots(tasks_old, tasks_new, CHECKPOINT))
    assert event["details"] == {"part": "task", "whitespace": "trailing carriage return"}


def test_name_whitespace_is_named_and_a_trimmed_verb_name_stays_one_verb():
    old = load(str(SUSPENDED))
    new = deepcopy(old)
    old.objects[2].name = " The First Room "
    old.objects[2].verbs[0].name = "eval "
    events = compare_snapshots(old, new).events
    assert [(e["kind"], e["new_path"][-1]["name"], e["details"]["whitespace"]) for e in events] == [
        ("changed", "name", "leading or trailing whitespace"), ("changed", "name", "leading or trailing whitespace")]
    other = deepcopy(new)
    other.objects[2].name = "Another Room"
    [event] = compare_snapshots(new, other).events
    assert "whitespace" not in event["details"]


def test_redaction_still_conceals_task_values_by_default():
    from lambdamoo_db.diff_redaction import RedactionPolicy
    from lambdamoo_db.diff_types import canonical_json
    old = two_tasks()
    new = deepcopy(old)
    frame = new.suspendedTasks[0].vm.stack[0]
    frame.rtEnv = {**frame.rtEnv, "secret": "TOPSECRET_VALUE"}
    frame.pc += 1
    policy = RedactionPolicy.from_data({"schema_version": 1, "rules": []})
    result = policy.bind(old, new, CHECKPOINT).apply(compare_snapshots(old, new, CHECKPOINT))
    assert b"TOPSECRET" not in canonical_json({"header": result.header, "events": result.events, "footer": result.footer})
    counts = result.footer["runtime_changes"]
    assert counts["task"] == counts["vm_private"] == 0 and counts["other"] > 0


def write(db, name, folder):
    path = Path(folder) / name
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    path.write_bytes(output.getvalue().encode("latin-1"))
    return path


def test_cli_reports_runtime_changes_by_part(tmp_path):
    old = two_tasks()
    new = deepcopy(old)
    new.suspendedTasks.reverse()
    new.suspendedTasks[0].vm.stack[0].pc += 1
    old_path, new_path = write(old, "old.db", tmp_path), write(new, "new.db", tmp_path)
    runner = CliRunner()
    text = runner.invoke(moodb, ["--db", str(new_path), "diff", str(old_path), "--view", "checkpoint"])
    assert text.exit_code == 1, text.output
    assert 'Runtime changes: {"other":0,"task":0,"vm_private":1}' in text.output
    assert '"suspendedTasks":{"matched":2,"new_only":0,"old_only":0}' in text.output
    jsonl = runner.invoke(moodb, ["--db", str(new_path), "diff", str(old_path), "--view", "checkpoint", "--format", "jsonl"])
    footer = json.loads(jsonl.output.splitlines()[-1])
    assert footer["runtime_changes"] == {"task": 0, "vm_private": 1, "other": 0}
    world = runner.invoke(moodb, ["--db", str(new_path), "diff", str(old_path)])
    assert world.exit_code == 0 and "Runtime changes" not in world.output
