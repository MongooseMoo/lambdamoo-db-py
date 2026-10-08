from copy import deepcopy
import struct

import attrs
import pytest

from lambdamoo_db.compare import compare_snapshots
from lambdamoo_db.database import (CLEAR, Activation, Anon, MooDatabase, MooObject, Property,
                                  MooCatch, MooFinally, ObjNum, VM, Verb, Waif, WaifReference)
from lambdamoo_db.diff_types import ReportOptions, canonical_json


def db(*objects, waifs=None):
    result = MooDatabase()
    result.version = 17
    result.versionstring = '** LambdaMOO Database, Format Version 17 **'
    result.objects = {o.id: o for o in objects}
    result.waifs = waifs or {}
    result.total_objects = len(objects)
    result.total_verbs = sum(v.code is not None for o in objects for v in o.verbs)
    return result


def obj(n=1, values=(), names=None, parents=()):
    result = MooObject(n, f'object{n}', 0, n, -1, list(parents))
    names = [f'p{i}' for i in range(len(values))] if names is None else names
    result.properties = [Property(name, value, n, 0) for name, value in zip(names, values)]
    result.propdefs_count = len(values)
    return result


def verb(o, name='test', code=None, perms=0, preps=-1):
    result = Verb(name, 1, perms, preps, o.id)
    result.code = code
    o.verbs.append(result)
    return result


def test_self_reversal_determinism_and_exact_float():
    old = db(obj(values=[0.0, 2**80, CLEAR, None]))
    assert compare_snapshots(old, old).footer['status'] == 'equal'
    new = deepcopy(old)
    new.objects[1].properties[0].value = -0.0
    result = compare_snapshots(old, new)
    assert result.exit_code == 1
    assert len(result.events) == 1
    assert result.events[0]['old']['bits'] == '0000000000000000'
    assert result.events[0]['new']['bits'] == '8000000000000000'
    assert canonical_json(result.events) == canonical_json(compare_snapshots(old, new).events)
    assert compare_snapshots(new, old).events[0]['old'] == result.events[0]['new']


def test_source_exact_hunks_full_payload_and_none_empty():
    a, b = obj(), obj()
    verb(a, code=['one', ' x ', '', 'tail'])
    verb(b, code=['one', ' x', '', 'tail', 'added'])
    result = compare_snapshots(db(a), db(b), ReportOptions(view='code', context=0))
    assert len(result.events) == 1
    event = result.events[0]
    assert event['old']['items'][1]['value'] == ' x '
    assert event['details']['added_lines'] == 2
    assert event['details']['removed_lines'] == 1
    assert event['details']['hunks']
    a.verbs[0].code, b.verbs[0].code = None, []
    assert compare_snapshots(db(a), db(b)).exit_code == 1


def test_duplicate_verb_signature_order_and_invalid():
    a, b = obj(), obj()
    verb(a, 'dup', ['a']); verb(a, 'other', ['c']); verb(a, 'dup', ['b'])
    verb(b, 'other', ['c']); verb(b, 'dup', ['a']); verb(b, 'dup', ['b'])
    result = compare_snapshots(db(a), db(b))
    assert [e['kind'] for e in result.events] == ['order_changed']
    b.verbs[0].perms = 3 << 4
    result = compare_snapshots(db(a), db(b))
    assert result.exit_code == 2
    assert result.footer['errors']


def test_scalar_list_single_opcode_event_and_structured_replacement():
    a = db(obj(values=[[1, 2, 3]]))
    b = db(obj(values=[[0, 1, 2, 3]]))
    result = compare_snapshots(a, b)
    assert len(result.events) == 1
    assert result.events[0]['details']['opcodes'][0]['tag'] == 'insert'
    a.objects[1].properties[0].value = [[1]]
    b.objects[1].properties[0].value = [[1], [2]]
    assert len(compare_snapshots(a, b).events) == 1


def test_lifecycle_recycled_section_and_code_container():
    a = db(obj()); b = db()
    b.recycled_objects = {1}
    b.total_objects = 1
    result = compare_snapshots(a, b, ReportOptions(sections=('recycled',)))
    assert len(result.events) == 1
    assert result.events[0]['details']['old_state'] == 'present'
    assert result.events[0]['details']['new_state'] == 'recycled'
    c = db(obj())
    assert compare_snapshots(db(), c, ReportOptions(view='code')).exit_code == 0
    verb(c.objects[1], code=['x'])
    c.total_verbs = 1
    result = compare_snapshots(db(), c, ReportOptions(view='code'))
    assert len(result.events) == 1 and result.events[0]['section'] == 'verbs'


@pytest.mark.parametrize('section', ['properties', 'verbs'])
def test_lifecycle_excluded_object_still_reports_selected_content(section):
    a, b = db(), db(obj(values=['selected property']))
    verb(b.objects[1], code=['selected source']); b.total_verbs = 1
    result = compare_snapshots(a, b, ReportOptions(sections=(section,)))
    assert result.exit_code == 1
    assert len(result.events) == 1
    assert result.events[0]['section'] == section
    assert compare_snapshots(b, a, ReportOptions(sections=(section,))).events[0]['kind'] == 'removed'


def test_code_lifecycle_payload_excludes_stored_property_values():
    a, b = db(), db(obj(values=['MALICIOUS_PROPERTY_SENTINEL']))
    verb(b.objects[1], code=['selected source']); b.total_verbs = 1
    result = compare_snapshots(a, b, ReportOptions(view='code'))
    assert b'MALICIOUS_PROPERTY_SENTINEL' not in canonical_json(result.events)
    assert b'selected source' in canonical_json(result.events)


def test_lifecycle_object_only_payload_excludes_property_and_verbs():
    a, b = db(), db(obj(values=['EXCLUDED_PROPERTY_SENTINEL']))
    verb(b.objects[1], code=['EXCLUDED_SOURCE_SENTINEL']); b.total_verbs = 1
    result = compare_snapshots(a, b, ReportOptions(sections=('objects',)))
    assert b'EXCLUDED_PROPERTY_SENTINEL' not in canonical_json(result.events)
    assert b'EXCLUDED_SOURCE_SENTINEL' not in canonical_json(result.events)


def test_selected_property_lifecycle_excludes_unselected_sparse_heap_body():
    a = db()
    b = db(obj(values=[WaifReference(777)]), waifs={777: Waif(1, 1, [(6, 'EXCLUDED_HEAP_SENTINEL')])})
    for sections in (('objects',), ('properties',)):
        result = compare_snapshots(a, b, ReportOptions(sections=sections))
        assert result.exit_code == 1
        assert b'EXCLUDED_HEAP_SENTINEL' not in canonical_json(result.events)
    result = compare_snapshots(a, b, ReportOptions(sections=('heap',)))
    assert result.exit_code == 1
    assert b'EXCLUDED_HEAP_SENTINEL' in canonical_json(result.events)


def test_property_definition_insertion_fanout_clear_no_cascade():
    parent = obj(1, values=[10], names=['a'])
    child = obj(2, values=[CLEAR], names=['a'], parents=[1]); child.propdefs_count = 0
    a = db(parent, child)
    b = deepcopy(a)
    b.objects[1].properties.insert(0, Property('new', 0, 1, 0)); b.objects[1].propdefs_count = 2
    b.objects[2].properties.insert(0, Property('new', CLEAR, 2, 0))
    result = compare_snapshots(a, b)
    assert not any(e['category'] == 'values' and e['entity']['id'] == '2' for e in result.events)
    added = [e for e in result.events if e['kind'] == 'added' and e['section'] == 'properties']
    assert len(added) == 1
    assert added[0]['details']['affected_descendants'] == 1


def test_heap_renumbering_cycles_orphans_and_checkpoint():
    a = db(obj(values=[WaifReference(4)]), waifs={4: Waif(1, 1, [(0, WaifReference(4))]), 77: Waif(1, 1, [])})
    b = db(obj(values=[WaifReference(9)]), waifs={9: Waif(1, 1, [(0, WaifReference(9))]), 88: Waif(1, 1, [])})
    result = compare_snapshots(a, b)
    assert result.exit_code == 0
    assert result.footer['heap_correspondence']
    assert result.footer['orphan_heap_counts'] == {'old': 1, 'new': 1}
    assert compare_snapshots(a, b, ReportOptions(view='checkpoint')).exit_code == 1


def test_alias_conflict_two_pass_suppresses_arbitrary_winning_content():
    a = db(obj(values=[WaifReference(1), WaifReference(1)]), waifs={1: Waif(1, 1, [(0, 'old')])})
    b = db(obj(values=[WaifReference(2), WaifReference(3)]), waifs={2: Waif(1, 1, [(0, 'new')]), 3: Waif(1, 1, [(0, 'old')])})
    result = compare_snapshots(a, b)
    assert result.exit_code == 1
    assert not result.footer['identity_complete']
    assert [e['kind'] for e in result.events] == ['identity_conflict']
    assert len(result.events[0]['old']['nodes']) == 1


def test_unknown_heap_map_keys_unequal_heap_list_and_dangling():
    a = db(obj(values=[{WaifReference(1): 'x'}]), waifs={1: Waif(1, 1, [])})
    b = db(obj(values=[{WaifReference(2): 'x'}]), waifs={2: Waif(1, 1, [])})
    result = compare_snapshots(a, b)
    assert result.exit_code == 3
    assert result.footer['selected_changes'] == 0
    a.objects[1].properties[0].value = [WaifReference(1)]
    b.objects[1].properties[0].value = [WaifReference(2), WaifReference(2)]
    assert compare_snapshots(a, b).exit_code == 1
    assert not compare_snapshots(a, b).footer['identity_complete']
    b.objects[1].properties[0].value = WaifReference(404)
    assert compare_snapshots(a, b).exit_code == 2


def test_unresolved_reference_map_key_count_still_proves_difference():
    a = db(obj(values=[{WaifReference(1): 'x'}]), waifs={1: Waif(1, 1, [])})
    b = db(obj(values=[{WaifReference(2): 'x', WaifReference(3): 'y'}]), waifs={2: Waif(1, 1, []), 3: Waif(1, 1, [])})
    result = compare_snapshots(a, b)
    assert result.exit_code == 1
    assert result.footer['selected_changes'] == 1
    assert not result.footer['identity_complete']


def test_map_key_server_equivalence_and_checkpoint_order():
    a = db(obj(values=[{'Hello': 1, ObjNum(1): 2}]))
    b = db(obj(values=[{ObjNum(1): 2, 'HELLO': 1}]))
    assert compare_snapshots(a, b).exit_code == 0
    assert compare_snapshots(a, b, ReportOptions(view='checkpoint')).exit_code == 1
    a.objects[1].properties[0].value = {float('nan'): 1}
    assert compare_snapshots(a, a).exit_code == 2


@pytest.mark.parametrize('key', [None, CLEAR, (1,), MooCatch(1), MooFinally(1), 'embedded\x00key'])
def test_in_memory_illegal_server_map_keys_fail_explicitly(key):
    a = db(obj(values=[{key: 1}]))
    result = compare_snapshots(a, a)
    assert result.exit_code == 2
    assert result.footer['errors']


def test_runtime_unset_nested_and_serialization_exclusions():
    a, b = db(obj()), db(obj())
    a.connections, b.connections = ['old'], ['new']
    act = Activation()
    act.debug = 1
    a.clocks = [VM({'v': act}, [act])]
    b.clocks = [VM({'v': Activation()}, [Activation()])]
    assert compare_snapshots(a, b).exit_code == 0
    result = compare_snapshots(a, b, ReportOptions(view='checkpoint'))
    assert result.exit_code == 1
    assert any(e['old'] == {'type': 'int', 'value': '1'} and e['new'] == {'type': 'unset'} for e in result.events)


def test_stop_after_conservative_error_and_selective_roots():
    a = db(obj(1, values=[1]), obj(2, values=[1]))
    b = db(obj(1, values=[2]), obj(2, values=[2]))
    result = compare_snapshots(a, b, ReportOptions(stop_after=1))
    assert len(result.events) == 1 and result.exit_code == 2
    assert not result.footer['scan_complete']
    assert len(compare_snapshots(a, b, ReportOptions(object_ids=(2,))).events) == 1
    with pytest.raises(ValueError):
        compare_snapshots(a, b, ReportOptions(object_ids=(999,)))


def test_heap_namespace_sides_do_not_conflate_equal_local_ids():
    a = db(obj(values=[WaifReference(1), WaifReference(2)]), waifs={1: Waif(1, 1, []), 2: Waif(1, 1, [])})
    b = db(obj(values=[WaifReference(2), WaifReference(3)]), waifs={2: Waif(1, 1, []), 3: Waif(1, 1, [])})
    assert compare_snapshots(a, b).exit_code == 0


def test_heap_only_new_root_and_removed_root_have_one_finite_graph_event():
    a = db(obj(values=[None]))
    b = db(obj(values=[WaifReference(4)]), waifs={4: Waif(1, 1, [(0, WaifReference(4))])})
    result = compare_snapshots(a, b, ReportOptions(sections=('heap',)))
    assert len(result.events) == 1 and result.events[0]['new']['type'] == 'graph'
    assert result.exit_code == 1
    assert compare_snapshots(b, a, ReportOptions(sections=('heap',))).exit_code == 1


def test_shared_nested_heap_retains_every_root_for_redaction():
    waifs = {1: Waif(1, 1, [(0, WaifReference(2))]), 2: Waif(1, 1, [(0, 'old')])}
    a = db(obj(values=[WaifReference(1), WaifReference(1)]), waifs=waifs)
    b = deepcopy(a); b.waifs[2].props = [(0, 'new')]
    result = compare_snapshots(a, b)
    event = result.events[0]
    root_slots = {anchor['old_path'][0]['slot'] for anchor in event['details']['anchors']}
    assert root_slots == {'0', '1'}


def test_nested_heap_report_ids_derive_from_anchor_without_raw_dump_ids():
    a = db(obj(values=[WaifReference(1)]), waifs={1: Waif(1, 1, [(0, WaifReference(2))]), 2: Waif(1, 1, [])})
    b = db(obj(values=[WaifReference(8)]), waifs={8: Waif(1, 1, [(0, WaifReference(9))]), 9: Waif(1, 1, [])})
    first = compare_snapshots(a, a).footer['heap_correspondence']
    second = compare_snapshots(a, b).footer['heap_correspondence']
    assert [row['id'] for row in first] == [row['id'] for row in second]


def test_missing_waif_class_is_unknown_and_visible():
    a = db(obj(values=[WaifReference(1)]), waifs={1: Waif(404, 1, [])})
    result = compare_snapshots(a, a)
    assert result.exit_code == 3
    assert result.events[0]['kind'] == 'identity_unresolved'


def test_field_registry_matches_live_models():
    from lambdamoo_db.compare import FIELD_COVERAGE, verify_field_coverage
    verify_field_coverage()
    for cls, coverage in FIELD_COVERAGE.items():
        assert set(coverage) == {field.name for field in attrs.fields(cls)}


def test_anonymous_renumber_own_definitions_and_cycles():
    aa = obj(7, values=[Anon(7)], names=['own']); aa.anon = True
    bb = obj(9, values=[Anon(9)], names=['own']); bb.anon = True
    aa.name = bb.name = 'anonymous'
    aa.owner = bb.owner = 1
    aa.properties[0].owner = bb.properties[0].owner = 1
    a, b = db(obj(values=[Anon(7)]), aa), db(obj(values=[Anon(9)]), bb)
    a.total_objects = b.total_objects = 1
    result = compare_snapshots(a, b)
    assert result.exit_code == 0
    bb.properties[0].value = 12
    assert compare_snapshots(a, b).exit_code == 1


def test_inserted_verb_definition_reports_its_new_coordinate_without_reorder():
    a, b = obj(), obj()
    verb(a, 'existing', ['same']); verb(b, 'inserted', ['new']); verb(b, 'existing', ['same'])
    result = compare_snapshots(db(a), db(b))
    assert [e['kind'] for e in result.events] == ['added']
    assert result.events[0]['new_path'][0]['slot'] == '0'


def test_malformed_python_cycle_and_nan_added_map_fail_explicitly():
    loop = []; loop.append(loop)
    a = db(obj(values=[loop]))
    assert compare_snapshots(a, a).exit_code == 2
    a = db(obj(values=[None]))
    b = db(obj(values=[{float('nan'): 'x'}]))
    assert compare_snapshots(a, b).exit_code == 2


def test_checkpoint_property_name_is_definition_category():
    a, b = db(obj(values=[1], names=['old'])), db(obj(values=[1], names=['new']))
    result = compare_snapshots(a, b, ReportOptions(view='checkpoint'))
    name_event = next(e for e in result.events if e['old_path'][-1] == {'type': 'field', 'name': 'propertyName'})
    assert name_event['category'] == 'definitions'


def test_parent_permutation_is_explicit_order_change_without_properties():
    a = db(obj(1, parents=[2, 3]), obj(2), obj(3))
    b = db(obj(1, parents=[3, 2]), obj(2), obj(3))
    result = compare_snapshots(a, b)
    assert len(result.events) == 1 and result.events[0]['kind'] == 'order_changed'


def test_checkpoint_dangling_heap_in_runtime_is_error():
    a = db(obj())
    a.pending_values = [Anon(404)]
    assert compare_snapshots(a, a).exit_code == 0
    result = compare_snapshots(a, a, ReportOptions(view='checkpoint'))
    assert result.exit_code == 2
    assert result.footer['errors'][0]['message'] == 'dangling heap reference'


def test_scoped_heap_orphans_never_count_dangling_negative():
    a = db(obj(values=[WaifReference(404)]))
    result = compare_snapshots(a, a)
    assert result.exit_code == 2
    assert result.footer['orphan_heap_counts'] == {'old': 0, 'new': 0}


@pytest.mark.parametrize('field', ['temp_this', 'temp_vloc', 'temp_value', 'temp_end', 'stack', 'rtEnv',
                                   'bi_func_data', 'language_version', 'argstr', 'dobjstr', 'prepstr', 'iobjstr',
                                   'unused1', 'unused2', 'unused3', 'unused4', 'pc', 'bi_func', 'error'])
def test_activation_stored_field_coverage(field):
    a, b = db(obj()), db(obj())
    activation = Activation()
    changed = Activation()
    value = getattr(changed, field)
    setattr(changed, field, ['changed'] if isinstance(value, list) else {'name': 'changed'} if isinstance(value, dict)
            else 'changed' if isinstance(value, str) else 41)
    a.clocks, b.clocks = [activation], [changed]
    result = compare_snapshots(a, b, ReportOptions(view='checkpoint'))
    assert result.exit_code == 1
    assert any(step == {'type': 'field', 'name': field} for e in result.events for step in e['old_path'])
