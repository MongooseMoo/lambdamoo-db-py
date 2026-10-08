"""Regression contracts for persisted slots and saved execution state."""

from copy import deepcopy
from pathlib import Path

import pytest

from lambdamoo_db.compare import DiffKind, DiffPath, compare_databases, compare_properties
from lambdamoo_db.database import Activation, InterruptedTask, MooDatabase, ObjNum, Property, QueuedTask, SuspendedTask, VM
from lambdamoo_db.reader import load


def saved_db():
    db = MooDatabase()
    db.version = 17
    db.versionstring = "test"
    activation = Activation()
    activation.debug = 1
    activation.verb = "run"
    activation.verbname = "run"
    activation.rtEnv = {"targets": [ObjNum(5)]}
    activation.bi_func_name = "call_function"
    activation.bi_func_data = ["nested builtin state"]
    vm = VM({"budget": 7}, [activation, None])
    queued = QueuedTask(1, 10, 20)
    queued.activation = deepcopy(activation)
    queued.rtEnv = {"target": ObjNum(5)}
    db.queuedTasks = [queued]
    suspended = SuspendedTask(1, 11, 21)
    suspended.vm = deepcopy(vm)
    db.suspendedTasks = [suspended]
    interrupted = InterruptedTask(12, "interrupted")
    interrupted.vm = deepcopy(vm)
    db.interruptedTasks = [interrupted]
    return db


def test_real_dump_reports_changed_suspended_task_id():
    original = load(str(Path(__file__).parents[1] / "toast2.db"))
    changed = deepcopy(original)
    changed.suspendedTasks[0].id += 1
    diffs = compare_databases(original, changed).diffs
    assert [str(d.path) for d in diffs] == ["suspendedTasks[0].id"]
    assert diffs[0].expected == original.suspendedTasks[0].id


@pytest.mark.parametrize("field", ["queuedTasks", "suspendedTasks", "interruptedTasks"])
def test_each_task_queue_reports_changed_ids_and_respects_ignore(field):
    original = saved_db()
    changed = deepcopy(original)
    getattr(changed, field)[0].id += 1
    assert [str(d.path) for d in compare_databases(original, changed)] == [f"{field}[0].id"]
    assert compare_databases(original, changed, ignore_fields={field}).identical


def test_nested_vm_and_activation_differences_have_precise_paths_and_limit():
    original = saved_db()
    changed = deepcopy(original)
    changed.queuedTasks[0].rtEnv["target"] = ObjNum(6)
    changed.suspendedTasks[0].vm.locals["budget"] = 8
    changed.suspendedTasks[0].vm.stack[0].pc = 42
    changed.interruptedTasks[0].vm.stack[0].bi_func_data[0] = "changed state"
    paths = [str(d.path) for d in compare_databases(original, changed)]
    assert paths == [
        "queuedTasks[0].rtEnv.target",
        "suspendedTasks[0].vm.locals.budget",
        "suspendedTasks[0].vm.stack[0].pc",
        "interruptedTasks[0].vm.stack[0].bi_func_data[0]",
    ]
    assert len(compare_databases(original, changed, max_diffs=2)) == 2


def test_optional_task_fields_distinguish_unset_from_none():
    original = saved_db()
    changed = deepcopy(original)
    del original.queuedTasks[0].activation
    changed.queuedTasks[0].activation = None
    diffs = compare_databases(original, changed).diffs
    assert len(diffs) == 1
    assert str(diffs[0].path) == "queuedTasks[0].activation"
    assert diffs[0].kind == DiffKind.EXTRA


def test_identical_saved_state_with_unset_fields_compares_equal():
    original = saved_db()
    changed = saved_db()
    del original.queuedTasks[0].activation
    del changed.queuedTasks[0].activation
    assert compare_databases(original, changed).identical


def test_removed_and_added_tasks_report_the_queue_position():
    original = saved_db()
    changed = deepcopy(original)
    changed.suspendedTasks.clear()
    diffs = compare_databases(original, changed).diffs
    assert [(str(d.path), d.kind) for d in diffs] == [
        ("suspendedTasks", DiffKind.LENGTH_MISMATCH), ("suspendedTasks[0]", DiffKind.MISSING),
    ]
    reverse = compare_databases(changed, original).diffs
    assert reverse[-1].kind == DiffKind.EXTRA


def test_v4_object_linkage_is_compared():
    original = load(str(Path(__file__).parents[1] / "tests" / "fixtures" / "Suspended.db"))
    changed = deepcopy(original)
    obj = next(iter(changed.objects.values()))
    obj.v4_sibling += 1
    assert [str(d.path) for d in compare_databases(original, changed)] == [f"#{obj.id}.v4_sibling"]


@pytest.mark.parametrize("field,value", [
    ("total_verbs", 1), ("total_players", 1), ("clocks", ["clock record"]),
    ("connections", ["connection record"]), ("connections_with_listeners", ""),
    ("has_connections_section", True), ("line_ending", "\r\n"), ("v4_dummy", "1"),
])
def test_persisted_header_and_connection_changes_are_reported(field, value):
    original = saved_db()
    changed = deepcopy(original)
    setattr(changed, field, value)
    diffs = compare_databases(original, changed).diffs
    assert diffs and all(str(d.path).startswith(field) for d in diffs)
    assert compare_databases(original, changed, ignore_fields={field}).identical


def test_duplicate_property_names_keep_both_slots_and_precise_paths():
    original = [Property("same", 1, 0, 5), Property("same", 2, 0, 5)]
    changed = [Property("same", 9, 0, 5), Property("same", 8, 0, 5)]
    diffs = compare_properties(DiffPath.object(1), original, changed)
    assert [str(d.path) for d in diffs] == ["#1.properties[0].value", "#1.properties[1].value"]
    assert [(d.expected, d.actual) for d in diffs] == [(1, 9), (2, 8)]


def test_removing_duplicate_property_preserves_the_missing_slot():
    original = [Property("same", 1, 0, 5), Property("same", 2, 0, 5)]
    diffs = compare_properties(DiffPath.object(1), original, original[:1])
    assert [(str(d.path), d.kind) for d in diffs] == [("#1.properties[1]", DiffKind.MISSING)]


def test_property_slot_order_and_names_are_part_of_the_dump():
    original = [Property("first", 1, 0, 5), Property("second", 2, 0, 5)]
    diffs = compare_properties(DiffPath.object(1), original, original[::-1])
    assert "#1.properties[0].propertyName" in [str(d.path) for d in diffs]
    renamed = [Property("renamed", 1, 0, 5), original[1]]
    assert [str(d.path) for d in compare_properties(DiffPath.object(1), original, renamed)] == ["#1.properties[0].propertyName"]
