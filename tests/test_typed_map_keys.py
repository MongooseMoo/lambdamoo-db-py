from io import StringIO
from pathlib import Path

from lambdamoo_db.database import MooError, ObjNum
from lambdamoo_db.reader import load
from lambdamoo_db.writer import Writer


def test_typed_scalar_map_keys_round_trip_byte_identically():
    fixture = Path(__file__).parent / "fixtures" / "TypedMapKeys.db"
    db = load(str(fixture))
    prop = next(
        prop
        for prop in db.objects[0].properties
        if prop.propertyName == "map_test"
    )

    assert len(prop.value) == 3
    assert [
        (type(key), int(key), value)
        for key, value in prop.value.items()
    ] == [
        (int, 1, "int"),
        (ObjNum, 1, "obj"),
        (MooError, 1, "err"),
    ]

    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()

    assert output.getvalue().encode("latin-1") == fixture.read_bytes()
