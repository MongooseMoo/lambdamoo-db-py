"""JSON projections must support strongly typed MOO scalars."""

import json
from io import StringIO
from pathlib import Path

import pytest

from lambdamoo_db.database import CLEAR, Anon, MooCatch, MooError, MooFinally, ObjNum
from lambdamoo_db import exporter
from lambdamoo_db.reader import load


def test_json_projection_handles_typed_parents_and_nested_map_keys():
    projection = {
        "parents": [ObjNum(2585)],
        "properties": [{ObjNum(3010): [Anon(2), MooError(1), MooCatch(3), MooFinally(4)]}],
    }
    data = exporter.to_json_data(projection)
    assert data == {"parents": [2585], "properties": [{3010: [2, 1, 3, 4]}]}
    assert json.loads(json.dumps(data))["parents"] == [2585]
    assert projection["parents"] == [ObjNum(2585)]


def test_json_string_and_file_accept_projections():
    projection = {"parents": [ObjNum(2585)]}
    stream = StringIO()
    exporter.to_json_file(projection, stream)
    assert json.loads(stream.getvalue()) == json.loads(exporter.to_json(projection)) == {
        "parents": [2585]
    }


def test_unsupported_json_values_do_not_silently_become_null():
    with pytest.raises(TypeError, match="object"):
        exporter.to_json({"unsupported": object()})


def test_json_preserves_recycled_ids_and_clear_export():
    assert json.loads(exporter.to_json({"recycled_objects": {42}, "value": CLEAR})) == {
        "recycled_objects": [42], "value": None
    }


def test_json_data_can_be_embedded_directly_with_clear_values():
    data = exporter.to_json_data({"values": [CLEAR, ObjNum(4)], "recycled": {42}})
    assert json.loads(json.dumps({"result": data})) == {
        "result": {"values": [None, 4], "recycled": [42]}
    }


def test_loaded_dump_exports_property_slots_verb_source_and_saved_vm_state():
    db = load(str(Path(__file__).parents[1] / "toast2.db"))
    data = json.loads(exporter.to_json(db))
    assert len(data["objects"]) == len(db.objects)
    obj = next(o for o in db.objects.values() if o.properties and any(v.code for v in o.verbs))
    exported = data["objects"][str(obj.id)]
    assert len(exported["properties"]) == len(obj.properties)
    assert [v["code"] for v in exported["verbs"]] == [v.code for v in obj.verbs]
    assert data["suspendedTasks"][0]["id"] == db.suspendedTasks[0].id
    assert data["suspendedTasks"][0]["vm"]["stack"][0]["pc"] == db.suspendedTasks[0].vm.stack[0].pc
    assert data["suspendedTasks"][0]["vm"]["stack"][0]["rtEnv"]
