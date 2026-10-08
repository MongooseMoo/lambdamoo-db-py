from copy import deepcopy

import pytest

from lambdamoo_db.compare import compare_snapshots
from lambdamoo_db.database import CLEAR, Anon, MooDatabase, MooObject, Property, Verb, Waif, WaifReference
from lambdamoo_db.diff_types import ReportOptions, canonical_json


def object_(number=1, values=(), parents=(), anon=False):
    result = MooObject(number, 'public', 0, 1, -1, list(parents), anon=anon)
    result.properties = [Property(f'p{i}', value, 1, 0) for i, value in enumerate(values)]
    result.propdefs_count = len(values)
    return result


def database(*objects, waifs=None):
    result = MooDatabase(objects={o.id: o for o in objects}, waifs=waifs or {})
    result.version = 17
    result.versionstring = 'synthetic'
    result.total_objects = sum(not o.anon for o in objects)
    result.total_verbs = sum(v.code is not None for o in objects for v in o.verbs)
    return result


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('selected', [None, (2,)])
def test_typed_player_membership_numeric_ids_order_and_selection(reverse, selected):
    from lambdamoo_db.database import ObjNum
    old = database(object_(1), object_(2), object_(10))
    new = deepcopy(old)
    old.players = [ObjNum(1), ObjNum(2)]; old.total_players = 2
    new.players = [ObjNum(10)]; new.total_players = 1
    if reverse:
        old, new = new, old
    result = compare_snapshots(old, new, ReportOptions(object_ids=selected, sections=('players',)))
    assert result.exit_code == 1
    assert [e['entity']['id'] for e in result.events] == (['1', '2', '10'] if selected is None else ['2'])
    expected = ['added', 'added', 'removed'] if reverse else ['removed', 'removed', 'added']
    if selected is not None:
        expected = expected[1:2]
    assert [e['kind'] for e in result.events] == expected


def test_code_view_does_not_traverse_excluded_property_layout(monkeypatch):
    from lambdamoo_db.compare import _SnapshotComparison
    source = database(object_(values=[{'many': list(range(1000))}]))
    def unexpected_layout(*args):
        pytest.fail('code view traversed an excluded property layout')
    monkeypatch.setattr(_SnapshotComparison, 'property_pairs', unexpected_layout)
    assert compare_snapshots(source, source, ReportOptions(view='code')).exit_code == 0


def test_player_order_uses_numeric_metadata_ids():
    from lambdamoo_db.database import ObjNum
    old = database(object_(1), object_(2)); new = deepcopy(old)
    old.players = [1, 2]; new.players = [ObjNum(1), ObjNum(2)]
    old.total_players = new.total_players = 2
    assert compare_snapshots(old, new, ReportOptions(sections=('players',))).exit_code == 0
    new.players.reverse()
    result = compare_snapshots(old, new, ReportOptions(sections=('players',)))
    assert result.exit_code == 1 and result.events[0]['kind'] == 'order_changed'


@pytest.mark.parametrize('reverse', [False, True])
def test_player_survivor_order_reported_with_membership_change(reverse):
    old = database(object_(2), object_(3), object_(4)); new = deepcopy(old)
    old.players = [2, 3]; old.total_players = 2
    new.players = [4, 3, 2]; new.total_players = 3
    if reverse:
        old, new = new, old
    result = compare_snapshots(old, new, ReportOptions(sections=('players',)))
    assert sorted(e['kind'] for e in result.events) == sorted(['removed' if reverse else 'added', 'order_changed'])


@pytest.mark.parametrize('selected', [(2,), (2, 3)])
def test_player_order_is_scoped_to_selected_ids(selected):
    old = database(object_(2), object_(3), object_(4)); new = deepcopy(old)
    old.players = [2, 3, 4]; new.players = [2, 4, 3]
    old.total_players = new.total_players = 3
    result = compare_snapshots(old, new, ReportOptions(sections=('players',), object_ids=selected))
    assert result.exit_code == 0 and not result.events


def test_player_scoped_reorder_evidence_excludes_unselected_ids():
    old = database(object_(2), object_(3), object_(4)); new = deepcopy(old)
    old.players = [2, 4, 3]; new.players = [3, 4, 2]
    old.total_players = new.total_players = 3
    result = compare_snapshots(old, new, ReportOptions(sections=('players',), object_ids=(2, 3)))
    assert result.exit_code == 1 and len(result.events) == 1
    assert [item['value'] for item in result.events[0]['old']['items']] == ['2', '3']
    assert [item['value'] for item in result.events[0]['new']['items']] == ['3', '2']


def test_duplicate_numeric_player_ids_are_an_error():
    from lambdamoo_db.database import ObjNum
    old = database(object_(2), object_(3)); new = deepcopy(old)
    old.players = [2, ObjNum(2), 3]; new.players = [2, 3, 3]
    old.total_players = new.total_players = 3
    result = compare_snapshots(old, new, ReportOptions(sections=('players',)))
    assert result.exit_code == 2 and not result.events
    assert any('duplicate player' in error['message'] for error in result.footer['errors'])


@pytest.mark.parametrize('invalid', [None, '2', True])
def test_player_metadata_rejects_non_object_ids(invalid):
    source = database(object_(2))
    source.players = [invalid]; source.total_players = 1
    result = compare_snapshots(source, source)
    assert result.exit_code == 2 and not result.events


def test_alias_merge_reference_keys_report_identity_conflict():
    a = database(object_(values=[WaifReference(1), WaifReference(2), {WaifReference(1): 1, WaifReference(2): 2}]),
                 waifs={1: Waif(1, 1, []), 2: Waif(1, 1, [])})
    b = database(object_(values=[WaifReference(5), WaifReference(5), {WaifReference(5): 1}]),
                 waifs={5: Waif(1, 1, [])})
    result = compare_snapshots(a, b)
    assert result.exit_code == 1
    assert any(e['kind'] == 'identity_conflict' and e['definite_change'] for e in result.events)
    assert not result.footer.get('errors')


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('graph', [False, True])
def test_checkpoint_heap_anonymous_lifecycle_full_record(reverse, graph):
    empty = database()
    full = database(object_(900, ['HEAP_PAYLOAD'], anon=True))
    if graph:
        full.objects[900].properties.append(Property('edge', WaifReference(3), 1, 0))
        full.objects[900].propdefs_count += 1
        full.waifs[3] = Waif(1, 1, [])
    a, b = (full, empty) if reverse else (empty, full)
    result = compare_snapshots(a, b, ReportOptions(view='checkpoint', sections=('heap',)))
    assert result.exit_code == 1
    assert b'HEAP_PAYLOAD' in canonical_json(result.events)
    payload = next(e for e in result.events if e['entity']['type'] == 'anonymous')['old' if reverse else 'new']
    fields = payload.get('root', payload)['fields']
    assert {'properties', 'verbs', 'parents', 'name', 'propdefs_count'} <= fields.keys()


@pytest.mark.parametrize('reverse', [False, True])
def test_verb_pure_add_remove_does_not_reorder_survivors(reverse):
    a = object_()
    a.verbs = [Verb('one', 1, 0, -1, 1)]
    b = deepcopy(a)
    b.verbs.append(Verb('two', 1, 0, -1, 1))
    old, new = (b, a) if reverse else (a, b)
    result = compare_snapshots(database(old), database(new))
    assert [e['kind'] for e in result.events] == ['removed' if reverse else 'added']


def test_inherited_definition_addition_has_one_definer_event():
    root = object_(values=[1, Anon(900)])
    children = [object_(n, [CLEAR, CLEAR], parents=[1], anon=n == 900) for n in (2, 3, 4, 900)]
    for child in children:
        child.propdefs_count = 0
    a = database(root, *children)
    b = deepcopy(a)
    b.objects[1].properties.append(Property('added', CLEAR, 1, 0))
    b.objects[1].propdefs_count += 1
    for child in children:
        b.objects[child.id].properties.append(Property('added', CLEAR, 1, 0))
    result = compare_snapshots(a, b)
    assert result.exit_code == 1
    assert len(result.events) == 1
    assert result.events[0]['details']['subtype'] == 'property_definition'
    assert result.events[0]['entity'] == {'type': 'permanent', 'id': '1'}


def test_survivor_reorder_with_added_definition_still_reports_order():
    a = object_(values=[1, 2])
    b = deepcopy(a)
    b.properties.reverse()
    b.properties.append(Property('added', 3, 1, 0))
    b.propdefs_count += 1
    result = compare_snapshots(database(a), database(b))
    assert {e['kind'] for e in result.events} == {'added', 'order_changed'}
