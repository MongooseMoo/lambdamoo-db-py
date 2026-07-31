from io import StringIO

from lambdamoo_db.database import Anon, MooDatabase
from lambdamoo_db.reader import Reader
from lambdamoo_db.writer import Writer


def test_pending_values_round_trip_without_data_loss():
    source = "1 values pending finalization\n12\n-1\n"
    db = MooDatabase()

    Reader(StringIO(source)).readPending(db)

    assert db.pending_values == [Anon(-1)]

    output = StringIO()
    Writer(db=db, output_file=output).writePending()
    assert output.getvalue() == source
