"""Property reference discovery must preserve MOO value identities."""

from io import StringIO
import json

import pytest

from lambdamoo_db.database import (
    Anon, MooCatch, MooDatabase, MooError, MooFinally, MooObject, ObjNum, Property,
)
from lambdamoo_db.reader import Reader
from lambdamoo_db.writer import Writer


@pytest.mark.parametrize("wrapper", [ObjNum, Anon, MooError, MooCatch, MooFinally])
def test_typed_scalars_have_type_sensitive_equality(wrapper):
    value = wrapper(1)
    assert value == wrapper(1)
    assert hash(value) == hash(wrapper(1))
    for other in [1, True, 1.0, ObjNum(1), Anon(1), MooError(1), MooCatch(1), MooFinally(1)]:
        if type(other) is not wrapper:
            assert not value == other
            assert not other == value
            assert value != other
            assert other != value


def test_reader_preserves_typed_map_keys_through_roundtrip():
    source = "10\n3\n0\n1\n2\ninteger\n1\n1\n2\nobject\n3\n1\n2\nerror\n"
    db = MooDatabase()
    value = Reader(StringIO(source)).readValue(db)
    assert len(value) == 3
    assert value[1] == "integer"
    assert value[ObjNum(1)] == "object"
    assert value[MooError(1)] == "error"
    output = StringIO()
    Writer(db=db, output_file=output).writeValue(value)
    assert output.getvalue() == source


def database_with_value(value):
    db = MooDatabase()
    obj = MooObject(0, "System", 0, 7, -1)
    obj.properties.append(Property("example", value, 7, 0))
    db.objects[0] = obj
    return db


def test_find_nested_references_in_lists_and_both_sides_of_maps():
    from lambdamoo_db.references import find_property_references

    value = [7, "#7", Anon(7), MooError(7), {7: "integer", ObjNum(7): [[ObjNum(7)]]}, ObjNum(7)]
    db = database_with_value(value)
    assert [str(path) for path in find_property_references(db, 7)] == [
        "#0.properties[0].value[4].entries[1].key",
        "#0.properties[0].value[4].entries[1].value[0][0]",
        "#0.properties[0].value[5]",
    ]
    assert db.objects[0].properties[0].value is value
    assert len(value[4]) == 2
    assert list(find_property_references(db, 99)) == []


def test_scalar_properties_and_shared_lists_are_reported_at_every_path():
    from lambdamoo_db.references import find_property_references

    shared = [ObjNum(7)]
    db = database_with_value([shared, shared])
    db.objects[0].properties.append(Property("direct", ObjNum(7), 0, 0))
    assert [str(path) for path in find_property_references(db, ObjNum(7))] == [
        "#0.properties[0].value[0][0]",
        "#0.properties[0].value[1][0]",
        "#0.properties[1].value",
    ]


def test_deep_nesting_does_not_use_python_recursion():
    from lambdamoo_db.references import find_property_references

    value = ObjNum(7)
    for _ in range(1500):
        value = [value]
    paths = list(find_property_references(database_with_value(value), 7))
    assert len(paths) == 1
    assert paths[0].segments == ("#0", "properties", 0, "value") + (0,) * 1500


def test_cycles_are_rejected_instead_of_looping_forever():
    from lambdamoo_db.references import find_property_references

    value = []
    value.append(value)
    with pytest.raises(ValueError, match="Cyclic property value"):
        list(find_property_references(database_with_value(value), 7))


def test_inherited_property_names_resolve_through_typed_parent_ids():
    db = database_with_value(ObjNum(7))
    child = MooObject(1, "Child", 0, 0, -1, [ObjNum(0)])
    child.properties.append(Property(None, ObjNum(7), 0, 0))
    db.objects[1] = child
    Reader(StringIO()).process_propnames(db, child)
    assert child.properties[0].propertyName == "example"


def test_json_export_keeps_existing_numeric_scalar_representation():
    from lambdamoo_db.exporter import to_json

    db = database_with_value([ObjNum(7), Anon(8), MooError(1)])
    db.version = 17
    db.versionstring = "** LambdaMOO Database, Format Version 17 **"
    db.objects[0].owner = ObjNum(0)
    document = json.loads(to_json(db))
    assert document["objects"]["0"]["owner"] == 0
    assert document["objects"]["0"]["properties"][0]["value"] == [7, 8, 1]


@pytest.mark.parametrize("oid", [True, 7.0, "7", Anon(7)])
def test_finder_rejects_other_target_types(oid):
    from lambdamoo_db.references import find_property_references

    with pytest.raises(TypeError, match="oid must"):
        list(find_property_references(MooDatabase(), oid))
