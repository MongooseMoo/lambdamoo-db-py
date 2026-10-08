from copy import deepcopy
import gc
import hashlib
import pytest

from lambdamoo_db.compare import _SnapshotComparison, compare_snapshots
from lambdamoo_db.database import MooDatabase, MooObject, Property, Waif, WaifReference
from lambdamoo_db.diff_types import ReportOptions, canonical_json


def chain(size=1200):
    root = MooObject(1, 'synthetic class', 0, 1, -1)
    root.properties = [Property('root', WaifReference(0), 1, 0)]
    root.propdefs_count = 1
    database = MooDatabase(objects={1: root}, waifs={n: Waif(1, 1, [(0, WaifReference(n + 1) if n + 1 < size else 'tail')]) for n in range(size)})
    database.version = 17
    database.versionstring = 'synthetic'
    database.total_objects = 1
    return database


def test_flat_1200_node_heap_chain_self_equality_and_changed_tail():
    database = chain()
    result = compare_snapshots(database, database)
    assert result.exit_code == 0
    assert len(result.footer['heap_correspondence']) == 1200
    assert all(len(row['anchors']) == 1 for row in result.footer['heap_correspondence'])
    assert all(len(row['anchors'][0]['old_path']) == 2 for row in result.footer['heap_correspondence'])
    changed = deepcopy(database)
    changed.waifs[1199].props = [(0, 'changed tail')]
    result = compare_snapshots(database, changed)
    assert result.exit_code == 1
    assert len(result.events) == 1
    assert result.events[0]['new'] == {'type': 'string', 'value': 'changed tail'}


def test_two_selected_property_roots_remain_two_anchors_through_long_chain():
    database = chain()
    database.objects[1].properties.append(Property('alias', WaifReference(0), 1, 0))
    database.objects[1].propdefs_count = 2
    result = compare_snapshots(database, database)
    assert result.exit_code == 0
    assert all(len(row['anchors']) == 2 for row in result.footer['heap_correspondence'])
    assert all({anchor['old_path'][0]['slot'] for anchor in row['anchors']} == {'0', '1'} for row in result.footer['heap_correspondence'])


def retained_path_step_references(comparison):
    """Count retained tagged-step references, including shared path storage."""
    pending = [e[side] for rows in comparison.proposals.values() for e in rows
               for side in ('old_path', 'new_path')]
    seen = set()
    count = 0
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, (list, tuple, dict)) or type(value).__module__ == 'lambdamoo_db.compare':
            children = gc.get_referents(value)
            count += sum(isinstance(child, dict) and 'type' in child for child in children)
            pending.extend(child for child in children if not isinstance(child, type))
    return count


def test_shared_chain_retains_linear_path_storage_and_all_root_aliases():
    database = chain(40)
    root = database.objects[1]
    root.properties = [Property('alias'+str(i), WaifReference(0), 1, 0) for i in range(40)]
    root.propdefs_count = 40
    comparison = _SnapshotComparison(database, database, ReportOptions())
    comparison.correspondence()
    assert len(comparison.pairs) == 40
    assert all(len(anchors) == 40 for anchors in comparison.anchors.values())
    # Forty roots * forty nodes retain constant work per discovered edge,
    # rather than retaining each complete ancestor path at every node.
    assert retained_path_step_references(comparison) < 6 * 40 * 40


def test_least_canonical_root_path_reaches_shared_descendants():
    database = chain(2)
    root = database.objects[1]
    values = [None] * 11
    values[2] = values[10] = WaifReference(0)
    root.properties[0].value = values
    comparison = _SnapshotComparison(database, database, ReportOptions())
    comparison.correspondence()
    entity = {'type': 'permanent', 'id': '1'}
    path = [{'type': 'property', 'slot': '0', 'definer': '1', 'name': 'root',
             'occurrence': '0', 'definition_position': '0'},
            {'type': 'field', 'name': 'value'}, {'type': 'index', 'index': '10'},
            {'type': 'heap', 'kind': 'waif', 'class': '1', 'slot': '0'}]
    expected = 'H' + hashlib.sha256(canonical_json({'entity': entity, 'old_path': path, 'new_path': path})).hexdigest()
    assert comparison.heap_ids[(('waif', 1), ('waif', 1))] == expected


@pytest.mark.parametrize('map_name', ['a', 'm', 'zz'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('independent', [False, True])
def test_reference_key_frontier_never_uses_a_root_order_winner(map_name, reverse, independent):
    old, new = chain(0), chain(0)
    for database, values in ((old, [('k', WaifReference(1)), (map_name, {WaifReference(1): WaifReference(10)}), ('z', WaifReference(2))]),
                             (new, [('k', WaifReference(5)), (map_name, {WaifReference(5): WaifReference(50)}), ('z', WaifReference(5))])):
        if independent:
            values.append(('public', WaifReference(10 if database is old else 50)))
        root = database.objects[1]
        root.properties = [Property(name, value, 1, 0) for name, value in sorted(values)]
        root.propdefs_count = len(values)
    old.waifs = {1: Waif(1, 1, []), 2: Waif(1, 1, []), 10: Waif(1, 1, [(0, 'old')])}
    new.waifs = {5: Waif(1, 1, []), 50: Waif(1, 1, [(0, 'new')])}
    if reverse:
        old, new = new, old
    result = compare_snapshots(old, new)
    rows = [row for row in result.footer['heap_correspondence']
            if (row['old_id'], row['new_id']) == (('50', '10') if reverse else ('10', '50'))]
    changes = [e for e in result.events if e['details'].get('subtype') == 'waif_property']
    assert len(rows) == len(changes) == int(independent)
    if independent:
        assert rows[0]['resolved']
        assert len(rows[0]['anchors']) == 1
        assert rows[0]['anchors'][0]['old_path'][0]['name'] == 'public'


def test_conditional_reference_key_conflict_is_uncertain_not_definite():
    old, new = chain(0), chain(0)
    old.objects[1].properties = [Property('k', WaifReference(1), 1, 0),
                                 Property('map', {WaifReference(1): WaifReference(2)}, 1, 0)]
    new.objects[1].properties = [Property('k', WaifReference(5), 1, 0),
                                 Property('map', {WaifReference(5): WaifReference(5)}, 1, 0)]
    for database in (old, new):
        database.objects[1].propdefs_count = 2
    old.waifs = {1: Waif(1, 1, []), 2: Waif(1, 1, [])}
    new.waifs = {5: Waif(1, 1, [])}
    for a, b in ((old, new), (new, old)):
        result = compare_snapshots(a, b)
        assert result.exit_code == 3
        assert not result.footer['identity_complete']
        assert not any(e['kind'] == 'identity_conflict' or e['definite_change'] for e in result.events)
        assert any(e['kind'] == 'identity_unresolved' for e in result.events)


@pytest.mark.parametrize('reverse', [False, True])
def test_canonical_diamond_paths_are_stable_under_heap_renumbering(reverse):
    old = chain(4)
    old.objects[1].properties[0].value = [None] * 11
    old.objects[1].properties[0].value[2] = WaifReference(0)
    old.objects[1].properties[0].value[10] = WaifReference(1)
    old.waifs[0].props = old.waifs[1].props = [(0, WaifReference(2))]
    new = deepcopy(old)
    new.objects[1].properties[0].value[2] = WaifReference(100)
    new.objects[1].properties[0].value[10] = WaifReference(101)
    new.waifs = {number + 100: waif for number, waif in new.waifs.items()}
    for waif in new.waifs.values():
        waif.props = [(slot, WaifReference(value.index + 100) if isinstance(value, WaifReference) else value)
                      for slot, value in waif.props]
    before = compare_snapshots(old, old)
    after = compare_snapshots(new, new)
    assert {row['id'] for row in before.footer['heap_correspondence']} == {row['id'] for row in after.footer['heap_correspondence']}
    new.waifs[103].props = [(0, 'changed tail')]
    a, b = (new, old) if reverse else (old, new)
    result = compare_snapshots(a, b)
    assert result.exit_code == 1
    assert len(result.events) == 1
    pair = _SnapshotComparison(a, b, ReportOptions())
    pair.correspondence()
    target = (('waif', 103), ('waif', 3)) if reverse else (('waif', 3), ('waif', 103))
    root_path = pair.roots[0][3]
    path = root_path + [{'type': 'index', 'index': '10'}]
    path += [{'type': 'heap', 'kind': 'waif', 'class': '1', 'slot': '0'}] * 2
    expected = 'H' + hashlib.sha256(canonical_json({'entity': pair.roots[0][2], 'old_path': path, 'new_path': path})).hexdigest()
    assert pair.heap_ids[target] == expected


@pytest.mark.parametrize('map_name', ['a', 'm', 'zz'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('independent', [False, True])
def test_later_map_value_key_conflict_removes_only_its_provisional_region(map_name, reverse, independent):
    old, new = chain(0), chain(0)
    old_values = [('key1', WaifReference(1)), ('key2', WaifReference(3)),
                  (map_name, {WaifReference(1): WaifReference(2)}),
                  ('secondmap', {WaifReference(3): WaifReference(4)})]
    new_values = [('key1', WaifReference(5)), ('key2', WaifReference(7)),
                  (map_name, {WaifReference(5): WaifReference(6)}),
                  ('secondmap', {WaifReference(7): WaifReference(5)})]
    if independent:
        old_values.append(('public', WaifReference(2)))
        new_values.append(('public', WaifReference(6)))
    for database, values in ((old, old_values), (new, new_values)):
        root = database.objects[1]
        root.properties = [Property(name, value, 1, 0) for name, value in sorted(values)]
        root.propdefs_count = len(values)
    old.waifs = {i: Waif(1, 1, [(0, 'old')] if i == 2 else []) for i in (1, 2, 3, 4)}
    new.waifs = {i: Waif(1, 1, [(0, 'new')] if i == 6 else []) for i in (5, 6, 7)}
    if reverse:
        old, new = new, old
    result = compare_snapshots(old, new)
    assert result.exit_code == 1
    rows = [row for row in result.footer['heap_correspondence']
            if (row['old_id'], row['new_id']) == (('6', '2') if reverse else ('2', '6'))]
    changes = [e for e in result.events if e['details'].get('subtype') == 'waif_property']
    assert len(rows) == len(changes) == int(independent)
    if independent:
        assert rows[0]['resolved'] and len(rows[0]['anchors']) == 1


def test_fixed_point_maps_propagate_nested_key_frontiers_and_root_aliases():
    old = chain(4)
    root = old.objects[1]
    root.properties = [Property('key', WaifReference(0), 1, 0),
                       Property('nested', {WaifReference(0): {WaifReference(1): WaifReference(3)}}, 1, 0)]
    root.propdefs_count = 2
    old.waifs[0].props = [(0, WaifReference(1))]
    old.waifs[1].props = []
    old.waifs[2].props = []
    old.waifs[3].props = [(0, WaifReference(0))]
    result = compare_snapshots(old, old)
    assert result.exit_code == 0
    rows = {int(row['old_id']): row for row in result.footer['heap_correspondence']}
    assert set(rows) == {0, 1, 3}
    assert all(row['resolved'] for row in rows.values())
    assert len(rows[0]['anchors']) == len(rows[1]['anchors']) == 2
    assert len(rows[3]['anchors']) == 1


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('cyclic', [False, True])
def test_reference_key_certificates_keep_map_only_heap_ids_stable(reverse, cyclic):
    old = chain(0)
    root = old.objects[1]
    root.properties = [Property('key', [WaifReference(1), WaifReference(2)], 1, 0),
                       Property('map', {WaifReference(1): WaifReference(3), WaifReference(2): WaifReference(4)}, 1, 0)]
    root.propdefs_count = 2
    old.waifs = {i: Waif(1, 1, []) for i in (1, 2, 3, 4, 5)}
    old.waifs[3].props = [(0, {WaifReference(3): WaifReference(5)})]
    old.waifs[5].props = [(0, {WaifReference(5): WaifReference(1)})] if cyclic else [(0, 'tail')]
    new = deepcopy(old)

    def renumber(value):
        if isinstance(value, WaifReference):
            return WaifReference(value.index + 100)
        if isinstance(value, list):
            return [renumber(item) for item in value]
        if isinstance(value, dict):
            return {renumber(key): renumber(item) for key, item in value.items()}
        return value

    for prop in new.objects[1].properties:
        prop.value = renumber(prop.value)
    new.waifs = {number + 100: waif for number, waif in new.waifs.items()}
    for waif in new.waifs.values():
        waif.props = [(slot, renumber(value)) for slot, value in waif.props]
    before = compare_snapshots(old, old)
    after = compare_snapshots(new, new)
    first = {int(row['old_id']): row['id'] for row in before.footer['heap_correspondence']}
    second = {int(row['old_id']) - 100: row['id'] for row in after.footer['heap_correspondence']}
    assert first == second
    assert len(set(first.values())) == 5
    a, b = (new, old) if reverse else (old, new)
    assert compare_snapshots(a, b).exit_code == 0
    a.waifs[105 if reverse else 5].props = [(0, 'changed')]
    result = compare_snapshots(a, b)
    assert result.exit_code == 1
    assert any(e['entity']['id'] == first[5] for e in result.events)


@pytest.mark.parametrize('reverse', [False, True])
def test_reference_key_certificates_do_not_publish_protected_key_anchors(reverse):
    from lambdamoo_db.diff_redaction import RedactionPolicy

    old = chain(0)
    root = old.objects[1]
    root.properties = [Property('SECRET_KEY_ANCHOR', WaifReference(777), 1, 0),
                       Property('public', {WaifReference(777): WaifReference(888)}, 1, 0)]
    root.propdefs_count = 2
    old.waifs = {777: Waif(1, 1, [(0, 'SECRET_KEY_BODY')]),
                 888: Waif(1, 1, [(0, 'public old')])}
    new = deepcopy(old)
    new.waifs[888].props = [(0, 'public new')]
    if reverse:
        old, new = new, old
    options = ReportOptions()
    comparison = compare_snapshots(old, new, options)
    assert any(row['old_id'] == '888' for row in comparison.footer['heap_correspondence'])
    policy = RedactionPolicy.from_data({'schema_version': 1, 'rules': [{'action': 'conceal',
        'selector': {'section': 'properties', 'definer': 1, 'name': 'SECRET_KEY_ANCHOR'}}]})
    redacted = policy.bind(old, new, options).apply(comparison)
    encoded = canonical_json({'header': redacted.header, 'events': redacted.events, 'footer': redacted.footer})
    assert b'SECRET_KEY' not in encoded
    assert b'777' not in encoded
