from io import StringIO
from pathlib import Path

import pytest

from lambdamoo_db.database import ObjNum, Property
from lambdamoo_db.objnums import remap
from lambdamoo_db.reader import load
from lambdamoo_db.rewrite import DEAD_BASE, RewriteRefused, dead, rewrite
from lambdamoo_db.writer import Writer

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"


def written(db) -> str:
    out = StringIO()
    Writer(db=db, output_file=out).writeDatabase()
    return out.getvalue()


def reread(db, tmp_path):
    path = tmp_path / "out.db"
    path.write_bytes(written(db).encode("latin-1"))
    return load(str(path))


def numbers(db):
    seen = []
    remap(db, lambda kind, n, where: seen.append((kind, n, where)) or n)
    return seen


def leaf(db):
    """A childless, non-player object with no verbs or properties owned by it elsewhere, located somewhere."""
    owners = {int(o.owner) for o in db.objects.values()}
    owners |= {int(v.owner) for o in db.objects.values() for v in o.verbs}
    owners |= {int(p.owner) for o in db.objects.values() for p in o.properties}
    players = {int(p) for p in db.players}
    return min(oid for oid, o in db.objects.items()
               if oid > 1 and not o.children and not o.contents and oid not in owners and oid not in players)


def test_a_compacted_dump_rewrites_to_the_same_bytes(tmp_path):
    db = load(str(TOASTCORE))
    assert db.recycled_objects  # so the first rewrite has gaps to close
    rewrite(db, [])
    once = reread(db, tmp_path)
    before = written(once)
    report = rewrite(once, [])
    assert written(once) == before
    assert all(old == new for old, new in report.renumbered.items())
    assert not report.dead_slots and not report.dropped


def test_removal_compacts_and_moves_every_reference(tmp_path):
    db = load(str(TOASTCORE))
    victim = leaf(db)
    holder = 0
    above = sorted(oid for oid in db.objects if oid > victim)
    db.objects[holder].properties.append(Property("pointers", [ObjNum(victim), ObjNum(above[0])], 2, 0))
    db.objects[holder].verbs[0].code = list(db.objects[holder].verbs[0].code) + [f"return {{#{victim}, #{above[0]}}};"]
    total = len(db.objects)
    report = rewrite(db, [victim])
    out = reread(db, tmp_path)
    assert len(out.objects) == total - 1 and out.total_objects == total - 1 and not out.recycled_objects
    assert report.renumbered[above[0]] == above[0] - 1
    assert out.objects[0].properties[-1].value == [ObjNum(dead(victim)), ObjNum(above[0] - 1)]
    assert out.objects[0].verbs[0].code[-1] == f"return {{#{dead(victim)}, #{above[0] - 1}}};"
    # nothing names a number past the end, and every negative number is #-1..#-3 or a dead one
    for kind, n, where in numbers(out):
        assert n < out.total_objects or kind == "anon", where
        assert n >= -3 or n <= DEAD_BASE, where


def test_set_lists_lose_dead_entries_including_already_missing_ones():
    baseline = rewrite(load(str(TOASTCORE)), [leaf(load(str(TOASTCORE)))]).dropped[".owned_objects"]
    db = load(str(TOASTCORE))
    victim = leaf(db)
    missing = max(db.objects) + 50
    db.objects[0].properties.append(Property("owned_objects", [ObjNum(victim), ObjNum(missing), ObjNum(1)], 2, 0))
    report = rewrite(db, [victim])
    assert db.objects[0].properties[-1].value == [ObjNum(report.renumbered[1])]
    assert report.dropped[".owned_objects"] == baseline + 2


def test_ownership_by_a_removed_object_refuses_without_owner_to():
    db = load(str(TOASTCORE))
    victim = leaf(db)
    db.objects[0].verbs[0].owner = ObjNum(victim)
    with pytest.raises(RewriteRefused, match="owned by removed objects"):
        rewrite(db, [victim])
    db = load(str(TOASTCORE))
    db.objects[0].verbs[0].owner = ObjNum(victim)
    report = rewrite(db, [victim], owner_to=2)
    assert int(db.objects[0].verbs[0].owner) == report.renumbered[2]
    assert report.reassigned["verb owner"] == 1


def test_empty_property_empties_the_holder_and_clears_descendants():
    from lambdamoo_db.database import CLEAR
    from lambdamoo_db.rewrite import _slot, empty_property

    db = load(str(TOASTCORE))
    parent = next(oid for oid, o in db.objects.items() if o.children and oid > 1)
    child = db.objects[int(db.objects[parent].children[0])]
    db.objects[parent].properties.insert(0, Property("baseline", {ObjNum(1): 2}, 2, 0))
    db.objects[parent].propdefs_count += 1
    for d in [child, *[db.objects[int(c)] for c in child.children]]:
        index = _slot(db, d, "baseline")
        d.properties.insert(index, Property("baseline", [ObjNum(3)], 2, 0))
    changed = empty_property(db, parent, "baseline")
    assert db.objects[parent].properties[0].value == {}
    assert child.properties[_slot(db, child, "baseline")].value is CLEAR
    assert changed >= 2
    with pytest.raises(RewriteRefused, match="no property"):
        empty_property(db, parent, "nonesuch")


def test_a_second_rewrite_keeps_old_dead_numbers_and_puts_new_ones_below_them():
    db = load(str(TOASTCORE))
    first = leaf(db)
    db.objects[0].properties.append(Property("first", ObjNum(first), 2, 0))
    one = rewrite(db, [first])
    old_dead = dead(first)
    second = leaf(db)
    db.objects[0].properties.append(Property("second", ObjNum(second), 2, 0))
    two = rewrite(db, [second])
    assert one.base == DEAD_BASE
    assert two.base < old_dead
    assert db.objects[0].properties[-2].value == ObjNum(old_dead)
    assert db.objects[0].properties[-1].value == ObjNum(dead(second, two.base))
