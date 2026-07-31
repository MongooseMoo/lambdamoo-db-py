from io import StringIO
from pathlib import Path

from lambdamoo_db.reader import load
from lambdamoo_db.writer import Writer


def test_minimal_v1_round_trip_is_byte_identical():
    fixture = Path(__file__).parent / "fixtures" / "Minimal.db"
    db = load(str(fixture))
    output = StringIO()

    Writer(db=db, output_file=output).writeDatabase()

    assert output.getvalue().encode("latin-1") == fixture.read_bytes()
