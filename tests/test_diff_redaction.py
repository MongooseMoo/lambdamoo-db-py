from copy import deepcopy
import json

import pytest

from lambdamoo_db.compare import compare_snapshots
from lambdamoo_db.database import Anon, MooDatabase, MooObject, ObjNum, Property, Verb, Waif, WaifReference
from lambdamoo_db.diff_types import ReportOptions, canonical_json
from lambdamoo_db.diff_redaction import RedactionPolicy


def db(value=None, name='public'):
    o = MooObject(1, name, 0, 1, -1)
    o.properties = [Property('secret', value, 1, 0)]
    o.propdefs_count = 1
    result = MooDatabase(objects={1: o})
    result.version = 17; result.versionstring = 'synthetic'; result.total_objects = 1
    return result


def policy(selector, action='conceal'):
    return RedactionPolicy.from_data({'schema_version': 1, 'rules': [{'selector': selector, 'action': action}]})


def apply(p, old, new, options=ReportOptions(), strict=False):
    return p.bind(old, new, options).apply(compare_snapshots(old, new, options), strict=strict)


def test_concealed_map_keys_and_counts_coalesce_without_original_mutation():
    a = db({'SECRET_KEY_1': 'SECRET_OLD', 'SECRET_KEY_2': 2})
    b = db({'SECRET_KEY_1': 'SECRET_NEW', 'SECRET_KEY_2': 3})
    p = policy({'section': 'properties', 'definer': '1', 'name': 'secret', 'occurrence': '*'})
    original = compare_snapshots(a, b)
    before = canonical_json(original.events)
    redacted = p.bind(a, b, ReportOptions()).apply(original)
    data = canonical_json({'header': redacted.header, 'events': redacted.events, 'footer': redacted.footer})
    assert b'SECRET' not in data
    assert len(redacted.events) == 1
    assert redacted.events[0]['old'] == {'type': 'redacted'}
    assert redacted.footer['selected_changes'] == 1
    assert redacted.events[0]['details']['concealed_uncertainty'] is False
    assert canonical_json(original.events) == before


def test_shared_heap_transitive_and_payload_lifecycle_do_not_leak():
    a = db(WaifReference(10)); a.waifs = {10: Waif(1, 1, [(0, WaifReference(20))]), 20: Waif(1, 1, [(0, 'SECRET_OLD')])}
    a.objects[1].properties.append(Property('public_alias', WaifReference(20), 1, 0)); a.objects[1].propdefs_count = 2
    b = deepcopy(a); b.waifs[20].props = [(0, 'SECRET_NEW')]
    p = policy({'section': 'properties', 'definer': '1', 'name': 'secret', 'occurrence': 0})
    result = apply(p, a, b)
    encoded = canonical_json({'events': result.events, 'footer': result.footer, 'header': result.header})
    assert b'SECRET' not in encoded
    assert b'public_alias' not in encoded
    assert not result.footer.get('heap_correspondence')
    empty = MooDatabase(); empty.version = 17; empty.versionstring = 'synthetic'
    result = apply(p, empty, b)
    assert b'SECRET' not in canonical_json(result.events)


def test_protected_heap_reference_map_key_coordinates_are_opaque():
    a = db(WaifReference(777)); a.waifs = {777: Waif(1, 1, [(0, 'SECRET_HEAP_BODY')])}
    a.objects[1].properties.append(Property('public', {WaifReference(777): 'old-public'}, 1, 0))
    a.objects[1].propdefs_count = 2
    b = deepcopy(a); b.objects[1].properties[1].value = {WaifReference(777): 'new-public'}
    result = apply(policy({'section': 'properties', 'definer': 1, 'name': 'secret', 'occurrence': 0}), a, b)
    encoded = canonical_json({'events': result.events, 'footer': result.footer})
    assert b'777' not in encoded
    assert b'SECRET_HEAP_BODY' not in encoded
    assert len(result.events) == 1


def test_labels_and_object_name_addition_concealed_everywhere():
    a, b = db(1, 'SECRET_OLD_NAME'), db(2, 'SECRET_NEW_NAME')
    p = policy({'section': 'objects', 'object': 1, 'field': 'name'})
    result = apply(p, a, b)
    assert b'SECRET' not in canonical_json(result.events)
    assert all(e['labels'] == {'old': None, 'new': None} for e in result.events)
    empty = MooDatabase(); empty.version = 17; empty.versionstring = 'synthetic'
    result = apply(p, empty, b)
    assert b'SECRET' not in canonical_json(result.events)


def test_source_whole_conceal_removes_line_counts_and_hunks():
    a, b = db(), db()
    for database, code in ((a, ['SECRET_OLD']), (b, ['SECRET_NEW', 'SECRET_MORE'])):
        v = Verb('exact aliases', 1, 0, -1, 1); v.code = code
        database.objects[1].verbs = [v]; database.total_verbs = 1
    result = apply(policy({'section': 'verbs', 'object': 1, 'name': 'exact aliases', 'occurrence': 0}), a, b)
    assert b'SECRET' not in canonical_json(result.events)
    assert 'hunks' not in result.events[0]['details']
    assert 'added_lines' not in result.events[0]['details']
    assert result.footer['added_source_lines'] == 0


def test_runtime_default_mask_and_explicit_reveal_conceal_wins():
    a, b = db(), db()
    a.connections, b.connections = ['SECRET_OLD'], ['SECRET_NEW']
    options = ReportOptions(view='checkpoint')
    p = RedactionPolicy.from_data({'schema_version': 1, 'rules': []})
    assert b'SECRET' not in canonical_json(apply(p, a, b, options).events)
    p = policy({'section': 'runtime'}, 'reveal_runtime')
    assert b'SECRET_NEW' in canonical_json(apply(p, a, b, options).events)
    p = RedactionPolicy.from_data({'schema_version': 1, 'rules': [
        {'selector': {'section': 'runtime'}, 'action': 'reveal_runtime'},
        {'selector': {'section': 'runtime'}, 'action': 'conceal'}]})
    assert b'SECRET' not in canonical_json(apply(p, a, b, options).events)


def test_strict_unchanged_match_and_zero_match_safe_error():
    a = db('UNCHANGED_SECRET')
    p = policy({'section': 'properties', 'definer': 1, 'name': 'secret', 'occurrence': 0})
    result = apply(p, a, a, strict=True)
    assert result.exit_code == 0
    assert result.footer['redaction_rules'][0]['matches'] > 0
    p = policy({'section': 'properties', 'definer': 1, 'name': 'PROTECTED_SELECTOR', 'occurrence': 0})
    result = apply(p, a, a, strict=True)
    assert result.exit_code == 2
    assert b'PROTECTED_SELECTOR' not in canonical_json({'header': result.header, 'footer': result.footer})


def test_property_layout_annotations_and_checkpoint_protected_heap_are_concealed():
    a = db('old'); b = db('new')
    a.objects[1].properties[0].propertyName = 'SECRET_DEFINITION'
    b.objects[1].properties[0].propertyName = 'SECRET_DEFINITION'
    b.objects[1].properties.insert(0, Property('public', 1, 1, 0)); b.objects[1].propdefs_count = 2
    p = policy({'section': 'properties', 'definer': 1, 'name': 'SECRET_DEFINITION', 'occurrence': 0})
    assert b'SECRET_DEFINITION' not in canonical_json(apply(p, a, b).events)
    a = db(WaifReference(10)); a.waifs = {10: Waif(1, 1, [(0, 'SECRET_OLD')])}
    b = deepcopy(a); b.waifs[10].props = [(0, 'SECRET_NEW')]
    options = ReportOptions(view='checkpoint')
    assert b'SECRET' not in canonical_json(apply(policy({'section': 'properties', 'definer': 1, 'name': 'secret', 'occurrence': 0}), a, b, options).events)


def test_unrelated_source_hunks_remain_available_under_property_rule():
    a, b = db('old'), db('new')
    for database, code in ((a, ['public old']), (b, ['public new'])):
        v = Verb('public', 1, 0, -1, 1); v.code = code
        database.objects[1].verbs = [v]; database.total_verbs = 1
    result = apply(policy({'section': 'properties', 'definer': 1, 'name': 'secret', 'occurrence': 0}), a, b)
    assert next(e for e in result.events if e['category'] == 'code')['details']['hunks']


def test_duplicate_selected_definitions_keep_distinct_concealed_roots():
    a, b = db('old0'), db('new0')
    for database, value in ((a, 'old1'), (b, 'new1')):
        database.objects[1].properties.append(Property('secret', value, 1, 0))
        database.objects[1].propdefs_count = 2
    result = apply(policy({'section': 'properties', 'definer': 1, 'name': 'secret', 'occurrence': '*'}), a, b)
    assert len(result.events) == 2
    assert result.footer['redacted_events'] == 2


def test_raw_slot_provenance_fallback_still_conceals_known_definition():
    a, b = db('SECRET_OLD'), db('SECRET_NEW')
    for database in (a, b):
        database.objects[1].properties.insert(0, Property(['nonstring synthesized name'], 1, 1, 0))
        database.objects[1].propdefs_count = 2
    result = apply(policy({'section': 'properties', 'definer': 1, 'name': 'secret', 'occurrence': 0}), a, b)
    assert b'SECRET' not in canonical_json(result.events)


def test_hidden_child_key_order_cannot_change_redacted_output():
    a = db({'secret z': 1, 'secret a': 2})
    b = db({'secret z': 3, 'secret a': 4})
    p = policy({'section': 'properties', 'definer': 1, 'name': 'secret', 'occurrence': 0})
    one = apply(p, a, b)
    a.objects[1].properties[0].value = {'completely different one': 1}
    b.objects[1].properties[0].value = {'completely different two': 2}
    two = apply(p, a, b)
    assert canonical_json(one.events) == canonical_json(two.events)


def test_section_rule_covers_payloads_in_parent_object_addition():
    a = MooDatabase(); a.version = 17; a.versionstring = 'synthetic'
    b = db('SECRET_PROPERTY')
    for selector in ({'section': 'properties'}, {'section': 'objects'}):
        result = apply(policy(selector), a, b)
        assert b'SECRET_PROPERTY' not in canonical_json(result.events)
    v = Verb('public', 1, 0, -1, 1); v.code = ['SECRET_SOURCE']
    b.objects[1].verbs = [v]; b.total_verbs = 1
    result = apply(policy({'section': 'verbs'}), a, b)
    assert b'SECRET_SOURCE' not in canonical_json(result.events)
    assert result.footer['added_source_lines'] == 0


def test_code_only_container_does_not_leak_excluded_properties_with_or_without_rule():
    a = MooDatabase(); a.version = 17; a.versionstring = 'synthetic'
    b = db('MALICIOUS_PROPERTY_SENTINEL')
    v = Verb('public', 1, 0, -1, 1); v.code = ['selected source']
    b.objects[1].verbs = [v]; b.total_verbs = 1
    options = ReportOptions(view='code')
    plain = compare_snapshots(a, b, options)
    redacted = apply(policy({'section': 'properties'}), a, b, options)
    assert b'MALICIOUS_PROPERTY_SENTINEL' not in canonical_json(plain.events)
    assert b'MALICIOUS_PROPERTY_SENTINEL' not in canonical_json(redacted.events)


def test_object_name_rule_masks_code_view_labels_even_when_objects_excluded():
    a, b = db(None, 'SECRET_OLD_LABEL'), db(None, 'SECRET_NEW_LABEL')
    for database, code in ((a, ['old source']), (b, ['new source'])):
        v = Verb('public', 1, 0, -1, 1); v.code = code
        database.objects[1].verbs = [v]; database.total_verbs = 1
    result = apply(policy({'section': 'objects', 'object': 1, 'field': 'name'}), a, b, ReportOptions(view='code'))
    assert b'SECRET' not in canonical_json(result.events)


def test_whole_objects_rule_masks_code_view_labels_and_bundle(tmp_path):
    from lambdamoo_db.diff_report import prepare_report, write_bundle
    a, b = db(None, 'SECRET_OLD_LABEL'), db(None, 'SECRET_NEW_LABEL')
    for database, code in ((a, ['old source']), (b, ['new source'])):
        v = Verb('public', 1, 0, -1, 1); v.code = code
        database.objects[1].verbs = [v]; database.total_verbs = 1
    options = ReportOptions(view='code')
    p = policy({'section': 'objects'}).bind(a, b, options)
    result = p.apply(compare_snapshots(a, b, options))
    assert b'SECRET' not in canonical_json(result.events)
    assert result.footer['redaction_rules'][0]['matches'] > 0
    report = prepare_report(result)
    destination = tmp_path / 'redacted-code'
    write_bundle(report, destination)
    assert all(b'SECRET' not in path.read_bytes() for path in destination.rglob('*') if path.is_file())


def test_aliases_builtin_selector_folds_ascii_spelling():
    a, b = db(['SECRET_ALIAS_OLD']), db(['SECRET_ALIAS_NEW'])
    a.objects[1].properties[0].propertyName = 'Aliases'
    b.objects[1].properties[0].propertyName = 'ALIASES'
    result = apply(policy({'section': 'objects', 'object': 1, 'field': 'aliases'}), a, b)
    assert b'SECRET_ALIAS' not in canonical_json(result.events)
    assert result.footer['redaction_rules'][0]['matches'] > 0


def test_whole_source_rule_does_not_conceal_unrelated_metadata():
    a, b = db(), db()
    for database in (a, b):
        v = Verb('public', 1, 0, -1, 1); v.code = ['unchanged']
        database.objects[1].verbs = [v]; database.total_verbs = 1
    b.objects[1].verbs[0].owner = 2
    result = apply(policy({'section': 'verbs', 'object': 1, 'name': 'public', 'occurrence': 0}), a, b)
    assert result.events[0]['old'] == {'type': 'int', 'value': '1'}
    assert result.events[0]['new'] == {'type': 'int', 'value': '2'}


def test_source_rule_in_added_object_masks_source_count():
    a = MooDatabase(); a.version = 17; a.versionstring = 'synthetic'
    b = db('public')
    v = Verb('public', 1, 0, -1, 1); v.code = ['SECRET_SOURCE']
    b.objects[1].verbs = [v]; b.total_verbs = 1
    result = apply(policy({'section': 'verbs', 'object': 1, 'name': 'public', 'occurrence': 0}), a, b)
    assert b'SECRET_SOURCE' not in canonical_json(result.events)
    assert result.footer['added_source_lines'] == 0


@pytest.mark.parametrize('selector,action', [({'section': 'INVALID_SECRET'}, 'conceal'),
    ({'section': 'objects', 'object': 1, 'field': 'owner'}, 'conceal'),
    ({'section': 'properties', 'definer': 1, 'name': 'x', 'occurrence': -1}, 'conceal'),
    ({'section': 'verbs', 'object': 1, 'name': 'x'}, 'reveal_runtime'),
    ({'section': 'runtime', 'unknown': 'SECRET'}, 'conceal')])
def test_rules_rejected_before_binding_without_secret_echo(selector, action):
    with pytest.raises(ValueError) as error:
        policy(selector, action)
    assert 'SECRET' not in str(error.value)


def test_waif_sparse_slot_rule_and_uncertainty_coalesce():
    a = db([WaifReference(10)]); a.waifs = {10: Waif(1, 1, [(3, {'SECRET_KEY': 'SECRET_OLD'})])}
    b = db([WaifReference(20), WaifReference(20)]); b.waifs = {20: Waif(1, 1, [(3, {'SECRET_NEW_KEY': 'SECRET_NEW'})])}
    result = apply(policy({'section': 'heap', 'waif_class': 1, 'slot': 3}), a, b)
    encoded = canonical_json({'events': result.events, 'footer': result.footer})
    assert b'SECRET' not in encoded
    assert result.exit_code == 1
    assert len(result.events) == 1
    assert result.events[0]['details']['concealed_uncertainty']
