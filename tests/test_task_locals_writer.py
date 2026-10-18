"""Task locals are written as ToastStunt's dbio_write_var writes them: every type, with its own tag.

The writer once looked task-local types up in a table with no errors, waifs or
anonymous objects: an error was written back as an integer and a waif raised.
"""
from io import StringIO
from pathlib import Path

from lambdamoo_db.database import MooError, Waif, WaifReference
from lambdamoo_db.reader import load, load_bytes
from lambdamoo_db.writer import Writer

FIXTURES = Path(__file__).parent / "fixtures"


def rewrite(db):
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    return load_bytes(output.getvalue().encode("latin-1"), "rewritten")


def test_suspended_task_locals_keep_type_and_value():
    db = load(str(FIXTURES / "Suspended.db"))
    db.waifs = {0: Waif(waif_class=0, owner=2, props=[])}
    frame = db.suspendedTasks[0].vm.stack[0]
    frame.rtEnv.update({"e": MooError(2), "w": WaifReference(0), "f": 1.5, "m": {"k": MooError(4)}})

    locals_ = rewrite(db).suspendedTasks[0].vm.stack[0].rtEnv

    assert locals_["e"] == MooError(2)
    assert locals_["f"] == 1.5
    assert locals_["m"] == {"k": MooError(4)}
    assert isinstance(locals_["w"], WaifReference)


def test_queued_task_locals_may_be_maps_keyed_by_objects():
    """The task and activation headers were formatted from a recursive asdict, which turns an ObjNum
    map key into a dict and raised "unhashable type: 'dict'" (seen on the live dump of 2026-10-18)."""
    from lambdamoo_db.database import ObjNum

    db = load(str(Path(__file__).parent.parent / "toastcore.db"))
    db.queuedTasks[0].rtEnv["seen"] = {ObjNum(5): 1}
    db.queuedTasks[0].activation.rtEnv["seen"] = {ObjNum(6): 2}

    task = rewrite(db).queuedTasks[0]

    assert task.rtEnv["seen"] == {ObjNum(5): 1}


def test_suspended_task_value_keeps_its_error_type():
    db = load(str(FIXTURES / "Suspended.db"))
    db.suspendedTasks[0].value = MooError(3)

    assert rewrite(db).suspendedTasks[0].value == MooError(3)
