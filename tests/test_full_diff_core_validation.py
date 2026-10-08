from unittest.mock import patch

from lambdamoo_db.compare import _SnapshotComparison, compare_snapshots
from lambdamoo_db.database import MooDatabase, MooObject, Property, Waif, WaifReference
from lambdamoo_db.diff_types import ReportOptions, encode_value


def shared_heap(size=1200):
    root = MooObject(1, 'synthetic shared heap', 0, 1, -1)
    refs = [WaifReference(n) for n in range(size)]
    root.properties = [Property('root', refs, 1, 0)]
    root.propdefs_count = 1
    database = MooDatabase(objects={1: root}, waifs={n: Waif(1, 1, [(0, refs)]) for n in range(size)})
    database.version = 17
    database.versionstring = 'synthetic'
    database.total_objects = 1
    return database


def test_structural_validation_never_serializes_whole_snapshot_or_heap_records():
    database = shared_heap()
    original = encode_value

    def scalar_only(value, db=None):
        assert not isinstance(value, (MooDatabase, Waif, list, tuple, dict, set)), 'validation serialized complete stored graph'
        assert db is None, 'validation expanded heap references'
        return original(value)

    comparison = _SnapshotComparison(database, database, ReportOptions(view='code'))
    with patch('lambdamoo_db.compare.encode_value', side_effect=scalar_only):
        comparison.validate()
    assert comparison.errors == []


def test_structural_validator_visits_each_shared_heap_record_once_per_side():
    import attrs
    database = shared_heap()
    original = attrs.fields
    visits = 0

    def counted(cls):
        nonlocal visits
        if cls is Waif:
            visits += 1
        return original(cls)

    comparison = _SnapshotComparison(database, database, ReportOptions(view='code'))
    with patch('lambdamoo_db.compare.attrs.fields', side_effect=counted):
        comparison.validate()
    # One registry check, plus every raw waif exactly once on each side. The
    # same 1200-entry list is shared by all records and must not multiply work.
    assert visits == 1 + 2 * len(database.waifs)
    assert comparison.errors == []


def test_validation_preserves_orphan_unsupported_type_and_direct_cycle_errors():
    database = shared_heap(3)
    database.waifs[999] = Waif(1, 1, [(0, object())])
    assert compare_snapshots(database, database, ReportOptions(view='code')).exit_code == 2
    loop = []; loop.append(loop)
    database.waifs[999].props = [(0, loop)]
    assert compare_snapshots(database, database, ReportOptions(view='code')).exit_code == 2


def test_shared_heap_graph_payload_has_one_node_per_side_record_and_repeatable_order():
    database = shared_heap(1200)
    for waif in database.waifs.values():
        waif.props = [(0, WaifReference(0))]
    payload = encode_value(database.objects[1].properties[0].value, database)
    assert len(payload['nodes']) == 1200
    assert [node['id'] for node in payload['nodes']] == [str(n) for n in range(1200)]
    # Avoid repeating the full dense payload: one bounded node still proves
    # that sharing stays references, rather than recursively expanded bodies.
    first = payload['nodes'][0]
    assert first['value']['fields']['props']['items'][0]['items'][1] == {'type': 'waif', 'value': '0'}


def test_selected_dangling_reference_remains_error_after_structural_validation():
    database = shared_heap(1)
    database.objects[1].properties[0].value = WaifReference(404)
    assert compare_snapshots(database, database).exit_code == 2
