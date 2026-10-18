"""An object's parents are written back in the form the dump stored them.

ToastStunt keeps parents as one object or as a list; a list of one or none is
kept as a list. The writer once chose the form from the count, so `{#2201}`
came back as `#2201`.
"""
from io import StringIO
from pathlib import Path

import pytest

from lambdamoo_db.reader import load, load_bytes
from lambdamoo_db.writer import Writer

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"  # v17: older formats store one parent only


def rewrite(db):
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    return load_bytes(output.getvalue().encode("latin-1"), "rewritten")


@pytest.mark.parametrize("parents", [[3], []], ids=["one", "none"])
def test_listed_parents_stay_a_list(parents):
    db = load(str(TOASTCORE))
    obj = db.objects[0]
    obj.parents, obj.parents_listed = parents, True

    again = rewrite(db).objects[0]

    assert again.parents_listed
    assert [int(p) for p in again.parents] == parents


def test_single_parent_stays_an_object():
    db = load(str(TOASTCORE))
    assert not db.objects[0].parents_listed

    assert not rewrite(db).objects[0].parents_listed
