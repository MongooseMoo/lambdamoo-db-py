from copy import deepcopy

import pytest

from lambdamoo_db.compare import compare_snapshots
from lambdamoo_db.database import CLEAR, Anon, MooDatabase, MooObject, Property, Verb, Waif, WaifReference
from lambdamoo_db.diff_redaction import RedactionPolicy
from lambdamoo_db.diff_report import prepare_report, render_text, write_bundle
from lambdamoo_db.diff_types import ReportOptions, canonical_json


def database(*objects, waifs=None):
    result = MooDatabase(objects={o.id: o for o in objects}, waifs=waifs or {})
    result.version = 17
    result.versionstring = 'synthetic'
    result.total_objects = sum(not o.anon for o in objects)
    return result


def policy(*rules):
    return RedactionPolicy.from_data({'schema_version': 1, 'rules': list(rules)})


def conceal(selector):
    return {'selector': selector, 'action': 'conceal'}


@pytest.mark.parametrize('reverse', [False, True])
def test_checkpoint_bare_waif_lifecycle_slot_concealed_in_artifacts(tmp_path, reverse):
    empty = database()
    full = database(waifs={3: Waif(5, 1, [(0, 'TOPSECRET')])})
    a, b = (full, empty) if reverse else (empty, full)
    options = ReportOptions(view='checkpoint', sections=('heap',))
    p = policy(conceal({'section': 'heap', 'waif_class': 5, 'slot': 0})).bind(a, b, options)
    report = prepare_report(compare_snapshots(a, b, options), redaction=p)
    assert b'TOPSECRET' not in canonical_json(report.events)
    assert 'TOPSECRET' not in render_text(report)
    write_bundle(report, tmp_path / 'bundle')
    assert all(b'TOPSECRET' not in path.read_bytes() for path in (tmp_path / 'bundle').rglob('*') if path.is_file())


def test_heap_rule_dangling_reference_bind_raises_safe_value_error():
    o = MooObject(1, 'public', 0, 1, -1)
    o.properties = [Property('p', WaifReference(7), 1, 0)]
    o.propdefs_count = 1
    source = database(o)
    p = policy(conceal({'section': 'heap', 'waif_class': 5, 'slot': 0}))
    with pytest.raises(ValueError, match='dangling heap reference'):
        p.bind(source, source)


def test_heap_section_conceals_runtime_only_waif_under_runtime_reveal():
    a = database(waifs={7: Waif(5, 1, [(0, 'TOPSECRET_OLD')])})
    a.connections = [WaifReference(7)]
    b = deepcopy(a)
    b.waifs[7].props = [(0, 'TOPSECRET_NEW')]
    b.connections = [WaifReference(7), 'public change']
    options = ReportOptions(view='checkpoint')
    p = policy({'selector': {'section': 'runtime'}, 'action': 'reveal_runtime'}, conceal({'section': 'heap'}))
    result = p.bind(a, b, options).apply(compare_snapshots(a, b, options))
    assert b'TOPSECRET' not in canonical_json({'header': result.header, 'events': result.events, 'footer': result.footer})
    assert p.match_counts[1] == 2


def test_property_raw_slot_fallback_conceals_uncertain_container():
    parent = MooObject(1, 'parent', 0, 1, -1)
    parent.properties = [Property('public', 1, 1, 0), Property('secret', 2, 1, 0)]
    parent.propdefs_count = 2
    child = MooObject(2, 'child', 0, 1, -1, [1])
    child.properties = [Property('unresolved', 'TOPSECRET_OLD', 1, 0)]
    a = database(parent, child)
    b = deepcopy(a)
    b.objects[2].properties[0].value = 'TOPSECRET_NEW'
    p = policy(conceal({'section': 'properties', 'definer': 1, 'name': 'secret'}))
    result = p.bind(a, b).apply(compare_snapshots(a, b))
    assert b'TOPSECRET' not in canonical_json({'header': result.header, 'events': result.events, 'footer': result.footer})
    assert any(e['details'].get('concealed_uncertainty') for e in result.events)


def inherited_anonymous_pair(lifecycle=False, heap_value=False):
    parent = MooObject(1, 'definer', 0, 1, -1)
    parent.properties = [Property('secret', CLEAR, 1, 0)]
    parent.propdefs_count = 1
    holder = MooObject(2, 'public', 0, 1, -1)
    holder.properties = [Property('public_ref', Anon(900), 1, 0), Property('public_alias', Anon(900), 1, 0)]
    holder.propdefs_count = 2
    anonymous = MooObject(900, 'anonymous', 0, 1, -1, [1], anon=True)
    anonymous.properties = [Property('secret', WaifReference(7) if heap_value else 'TOPSECRET_OLD', 1, 0)]
    a = database(parent, holder, anonymous, waifs={7: Waif(1, 1, [(0, 'TOPSECRET_OLD')])} if heap_value else {})
    b = deepcopy(a)
    if heap_value:
        b.waifs[7].props = [(0, 'TOPSECRET_NEW')]
        for source in (a, b):
            source.objects[2].properties.append(Property('waif_alias', WaifReference(7), 1, 0))
            source.objects[2].propdefs_count += 1
    else:
        b.objects[900].properties[0].value = 'TOPSECRET_NEW'
    if lifecycle:
        del a.objects[900]
        for prop in a.objects[2].properties[:2]:
            prop.value = None
        a.waifs = {}
        if heap_value:
            a.objects[2].properties[2].value = None
    return a, b


@pytest.mark.parametrize('options', [ReportOptions(), ReportOptions(view='checkpoint'),
    ReportOptions(object_ids=(2,)), ReportOptions(view='checkpoint', sections=('heap',)),
    ReportOptions(view='checkpoint', sections=('heap',), object_ids=(2,))])
@pytest.mark.parametrize('lifecycle', [False, True])
def test_inherited_anonymous_conceal_direct_graph_lifecycle_and_scope(tmp_path, options, lifecycle):
    a, b = inherited_anonymous_pair(lifecycle=lifecycle)
    p = policy(conceal({'section': 'properties', 'definer': 1, 'name': 'secret'})).bind(a, b, options)
    comp = compare_snapshots(a, b, options)
    assert comp.exit_code == 1
    expected = (2 if 'properties' in options.sections and options.object_ids is None else 0) + (1 if lifecycle else 2)
    assert p.match_counts == [expected]
    report = prepare_report(comp, redaction=p)
    assert b'TOPSECRET' not in canonical_json({'header': report.header, 'events': report.events, 'footer': report.footer})
    assert 'TOPSECRET' not in render_text(report)
    write_bundle(report, tmp_path / 'bundle')
    assert all(b'TOPSECRET' not in path.read_bytes() for path in (tmp_path / 'bundle').rglob('*') if path.is_file())


@pytest.mark.parametrize('view', ['world', 'checkpoint'])
def test_inherited_anonymous_secret_heap_aliases_are_transitively_concealed(view):
    a, b = inherited_anonymous_pair(heap_value=True)
    options = ReportOptions(view=view)
    p = policy(conceal({'section': 'properties', 'definer': 1, 'name': 'secret'})).bind(a, b, options)
    result = p.apply(compare_snapshots(a, b, options))
    assert b'TOPSECRET' not in canonical_json({'header': result.header, 'events': result.events, 'footer': result.footer})
    assert p.match_counts == [4]


def test_anonymous_property_layout_conceals_definition_annotations():
    a, b = inherited_anonymous_pair()
    for source in (a, b):
        source.objects[1].properties[0].propertyName = 'SECRET_DEFINITION'
        source.objects[1].properties.append(Property('public_definition', CLEAR, 1, 0))
        source.objects[1].propdefs_count += 1
        source.objects[900].properties.append(Property('public_definition', 'public', 1, 0))
    b.objects[1].properties.reverse()
    b.objects[900].properties.reverse()
    options = ReportOptions(object_ids=(2,))
    p = policy(conceal({'section': 'properties', 'definer': 1, 'name': 'SECRET_DEFINITION'})).bind(a, b, options)
    result = p.apply(compare_snapshots(a, b, options))
    assert b'SECRET' not in canonical_json({'header': result.header, 'events': result.events, 'footer': result.footer})


def assert_private_report(tmp_path, comparison, p):
    report = prepare_report(p.apply(comparison, strict=True))
    assert report.exit_code != 2
    assert b'TOPSECRET' not in canonical_json({'header': report.header, 'events': report.events, 'footer': report.footer})
    assert 'TOPSECRET' not in render_text(report)
    write_bundle(report, tmp_path / 'bundle')
    assert all(b'TOPSECRET' not in path.read_bytes() for path in (tmp_path / 'bundle').rglob('*') if path.is_file())
    return report


def waif_definition_pair(lifecycle='changed', inherited=False, shared=False):
    cls = MooObject(5, 'waif class', 0, 1, -1)
    cls.properties = [Property('ordinary', CLEAR, 1, 0), Property(':TOPSECRET_NAME', CLEAR, 1, 0)]
    cls.propdefs_count = 2
    child = MooObject(6, 'derived class', 0, 1, -1, [5])
    child.properties = [Property('own ordinary', CLEAR, 1, 0)] + deepcopy(cls.properties)
    child.propdefs_count = 1
    holder = MooObject(2, 'public', 0, 1, -1)
    holder.properties = [Property('root', WaifReference(3), 1, 0), Property('alias', WaifReference(3), 1, 0)]
    holder.propdefs_count = 2
    waifs = {3: Waif(6 if inherited else 5, 1, [(0, WaifReference(8) if shared else 'TOPSECRET_OLD')])}
    if shared:
        waifs[8] = Waif(5, 1, [(0, 'TOPSECRET_OLD')])
        holder.properties.append(Property('transitive_alias', WaifReference(8), 1, 0))
        holder.propdefs_count += 1
    a = database(cls, child, holder, waifs=waifs)
    b = deepcopy(a)
    b.waifs[8 if shared else 3].props = [(0, 'TOPSECRET_NEW')]
    if lifecycle != 'changed':
        a.waifs = {}
        for prop in a.objects[2].properties:
            prop.value = None
    return (b, a) if lifecycle == 'removed' else (a, b)


@pytest.mark.parametrize('options', [ReportOptions(), ReportOptions(view='checkpoint'),
    ReportOptions(object_ids=(2,)), ReportOptions(view='checkpoint', sections=('heap',)),
    ReportOptions(view='checkpoint', sections=('heap',), object_ids=(2,))])
@pytest.mark.parametrize('lifecycle', ['changed', 'added', 'removed'])
def test_property_definition_rule_covers_waif_slots_and_artifacts(tmp_path, options, lifecycle):
    a, b = waif_definition_pair(lifecycle=lifecycle)
    p = policy(conceal({'section': 'properties', 'definer': 5, 'name': ':TOPSECRET_NAME'})).bind(a, b, options)
    expected = (4 if 'properties' in options.sections and options.object_ids is None else 0) + (2 if lifecycle == 'changed' else 1)
    assert p.match_counts == [expected]
    assert_private_report(tmp_path, compare_snapshots(a, b, options), p)


@pytest.mark.parametrize('view', ['world', 'checkpoint'])
def test_waif_inherited_definition_transitive_public_aliases_concealed(tmp_path, view):
    a, b = waif_definition_pair(inherited=True, shared=True)
    options = ReportOptions(view=view, object_ids=(2,))
    p = policy(conceal({'section': 'properties', 'definer': 5, 'name': ':TOPSECRET_NAME'})).bind(a, b, options)
    assert p.match_counts == [4]
    result = assert_private_report(tmp_path, compare_snapshots(a, b, options), p)
    assert not result.footer['heap_correspondence']


@pytest.mark.parametrize('options', [ReportOptions(), ReportOptions(view='checkpoint'),
    ReportOptions(view='checkpoint', sections=('heap',), object_ids=(2,))])
@pytest.mark.parametrize('lifecycle', ['changed', 'added', 'removed'])
def test_whole_properties_section_covers_waif_stored_slots(tmp_path, options, lifecycle):
    a, b = waif_definition_pair(lifecycle=lifecycle, inherited=True)
    p = policy(conceal({'section': 'properties'})).bind(a, b, options)
    expected = sum(bool(source.waifs or 'properties' in options.sections and source.objects) for source in (a, b))
    assert p.match_counts == [expected]
    assert_private_report(tmp_path, compare_snapshots(a, b, options), p)


def anonymous_verb_pair(lifecycle='changed'):
    holder = MooObject(2, 'public', 0, 1, -1)
    holder.properties = [Property('root', Anon(900), 1, 0), Property('alias', Anon(900), 1, 0)]
    holder.propdefs_count = 2
    anonymous = MooObject(900, 'anonymous', 0, 1, -1, anon=True)
    for obj in (holder, anonymous):
        verb = Verb('TOPSECRET_VERB_NAME' if obj.anon else 'public', 1, 0, -1, obj.id)
        verb.code = ['TOPSECRET_OLD;']
        obj.verbs = [verb]
    a = database(holder, anonymous)
    a.total_verbs = 2
    b = deepcopy(a)
    for obj in b.objects.values():
        obj.verbs[0].code = ['TOPSECRET_NEW;', 'TOPSECRET_MORE;']
    if lifecycle != 'changed':
        del a.objects[900]
        a.total_verbs = 1
        for prop in a.objects[2].properties:
            prop.value = None
    return (b, a) if lifecycle == 'removed' else (a, b)


@pytest.mark.parametrize('options', [ReportOptions(), ReportOptions(view='checkpoint'),
    ReportOptions(object_ids=(2,)), ReportOptions(view='checkpoint', sections=('heap',)),
    ReportOptions(view='code')])
@pytest.mark.parametrize('lifecycle', ['changed', 'added', 'removed'])
def test_whole_verbs_section_covers_anonymous_sources_definitions_and_artifacts(tmp_path, options, lifecycle):
    a, b = anonymous_verb_pair(lifecycle)
    p = policy(conceal({'section': 'verbs'})).bind(a, b, options)
    expected = 2 if 'verbs' in options.sections or lifecycle == 'changed' else 1
    assert p.match_counts == [expected]
    result = assert_private_report(tmp_path, compare_snapshots(a, b, options), p)
    assert result.footer['added_source_lines'] == result.footer['removed_source_lines'] == 0


def test_anonymous_verb_whole_section_masks_order_annotations():
    a, b = anonymous_verb_pair()
    for source in (a, b):
        second = Verb('TOPSECRET_SECOND', 1, 0, -1, 900)
        second.code = ['TOPSECRET_SECOND_SOURCE']
        source.objects[900].verbs.append(second)
        source.total_verbs += 1
    b.objects[900].verbs.reverse()
    options = ReportOptions(object_ids=(2,))
    p = policy(conceal({'section': 'verbs'})).bind(a, b, options)
    result = p.apply(compare_snapshots(a, b, options))
    assert b'TOPSECRET' not in canonical_json(result.events)


def test_waif_definition_occurrence_compacts_nonwaif_definitions_and_preserves_public_slot():
    a, b = waif_definition_pair()
    for source, hidden, public in ((a, 'TOPSECRET_OLD', 'PUBLIC_OLD'), (b, 'TOPSECRET_NEW', 'PUBLIC_NEW')):
        cls = source.objects[5]
        cls.properties = [Property('ordinary', CLEAR, 1, 0), Property(':duplicate', CLEAR, 1, 0), Property(':duplicate', CLEAR, 1, 0)]
        cls.propdefs_count = 3
        source.objects[6].properties = [Property('own ordinary', CLEAR, 1, 0)] + deepcopy(cls.properties)
        source.waifs[3].props = [(0, hidden), (1, public)]
    options = ReportOptions(object_ids=(2,))
    p = policy(conceal({'section': 'properties', 'definer': 5, 'name': ':duplicate', 'occurrence': 0})).bind(a, b, options)
    result = p.apply(compare_snapshots(a, b, options), strict=True)
    assert p.match_counts == [2]
    data = canonical_json(result.events)
    assert b'TOPSECRET' not in data
    assert b'PUBLIC_NEW' in data


def test_anonymous_verb_rule_counts_only_selected_reachable_records():
    a, b = anonymous_verb_pair()
    for source in (a, b):
        source.objects[2].verbs = []
        source.total_verbs -= 1
        unrelated = MooObject(901, 'unselected anonymous', 0, 1, -1, anon=True)
        hidden = Verb('TOPSECRET_UNSELECTED', 1, 0, -1, 901)
        hidden.code = ['TOPSECRET_UNSELECTED_SOURCE']
        unrelated.verbs = [hidden]
        source.objects[901] = unrelated
        source.total_verbs += 1
    options = ReportOptions(view='checkpoint', object_ids=(2,))
    p = policy(conceal({'section': 'verbs'})).bind(a, b, options)
    result = p.apply(compare_snapshots(a, b, options), strict=True)
    assert p.match_counts == [2]
    assert result.exit_code == 1
    assert b'TOPSECRET' not in canonical_json(result.events)


def test_whole_verbs_without_selected_containers_fail_strict_matching():
    a, b = anonymous_verb_pair()
    for source in (a, b):
        source.objects[2].verbs = []
        source.total_verbs -= 1
    options = ReportOptions(sections=('objects',), object_ids=(2,))
    p = policy(conceal({'section': 'verbs'})).bind(a, b, options)
    result = p.apply(compare_snapshots(a, b, options), strict=True)
    assert p.match_counts == [0]
    assert result.exit_code == 2


def lifecycle_count_pair(kind, count, reverse=False, graph=False):
    public = MooObject(2, 'public old', 0, 1, -1)
    hidden = MooObject(900 if kind == 'anonymous-properties' else 3, 'public hidden holder', 0, 1, -1,
                       anon=kind == 'anonymous-properties')
    section = 'verbs' if kind == 'permanent-verbs' else 'properties'
    if section == 'properties':
        hidden.properties = [Property(f'TOPSECRET_NAME_{i}', f'TOPSECRET_VALUE_{i}', 1, 0) for i in range(count)]
        hidden.propdefs_count = count
    else:
        for i in range(count):
            verb = Verb(f'TOPSECRET_VERB_{i}', 1, 0, -1, hidden.id)
            verb.code = [f'TOPSECRET_SOURCE_{i}']
            hidden.verbs.append(verb)
    a = database(public)
    b = database(deepcopy(public), hidden)
    b.objects[2].name = 'public new'
    b.total_verbs = count if section == 'verbs' else 0
    if graph:
        for source, value in ((a, None), (b, Anon(hidden.id))):
            source.objects[2].properties = [Property('public_reference', value, 1, 0)]
            source.objects[2].propdefs_count = 1
    return ((b, a) if reverse else (a, b)), section


def bundle_bytes(report, destination):
    write_bundle(report, destination)
    return {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob('*') if path.is_file()}


def encoded_records(value):
    if isinstance(value, dict):
        if value.get('type') == 'record' and value.get('record_type') == 'MooObject':
            yield value
        for child in value.values():
            yield from encoded_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from encoded_records(child)


@pytest.mark.parametrize('kind,options,graph', [
    ('anonymous-properties', ReportOptions(view='checkpoint'), False),
    ('anonymous-properties', ReportOptions(view='checkpoint', sections=('heap', 'objects')), False),
    ('anonymous-properties', ReportOptions(sections=('heap', 'objects')), True),
    ('permanent-properties', ReportOptions(), False),
    ('permanent-properties', ReportOptions(view='checkpoint'), False),
    ('permanent-verbs', ReportOptions(), False),
    ('permanent-verbs', ReportOptions(view='checkpoint'), False),
])
@pytest.mark.parametrize('reverse', [False, True])
def test_whole_section_lifecycle_counts_are_content_independent_in_all_artifacts(tmp_path, kind, options, graph, reverse):
    reports, artifacts = [], []
    for count in (2, 5):
        (a, b), section = lifecycle_count_pair(kind, count, reverse, graph)
        p = policy(conceal({'section': section})).bind(a, b, options)
        report = prepare_report(p.apply(compare_snapshots(a, b, options), strict=True))
        assert report.exit_code == 1
        records = list(encoded_records(report.events))
        assert records
        target = next(record for record in records if record['fields']['id']['value'] == ('900' if kind == 'anonymous-properties' else '3'))
        assert target['fields'][section] == {'type': 'redacted'}
        if section == 'properties':
            assert target['fields']['propdefs_count'] == {'type': 'redacted'}
        data = canonical_json({'header': report.header, 'events': report.events, 'footer': report.footer})
        assert b'TOPSECRET' not in data
        assert b'public new' in data
        reports.append((data, render_text(report)))
        artifacts.append(bundle_bytes(report, tmp_path / f'count-{count}'))
    assert reports[0] == reports[1]
    assert artifacts[0] == artifacts[1]


def test_whole_properties_private_heap_growth_does_not_leak_through_match_counts_or_artifacts(tmp_path):
    reports, artifacts = [], []
    for count in (2, 5):
        holder = MooObject(2, 'public old', 0, 1, -1)
        holder.properties = [Property('private', [], 1, 0)]
        holder.propdefs_count = 1
        cls = MooObject(5, 'public class', 0, 1, -1)
        cls.properties = [Property(':private', CLEAR, 1, 0)]
        cls.propdefs_count = 1
        a = database(holder, cls)
        b = deepcopy(a)
        b.objects[2].name = 'public changed name'
        b.objects[2].properties[0].value = [WaifReference(number) for number in range(10, 10 + count)]
        b.waifs = {number: Waif(5, 1, [(0, f'TOPSECRET_{number}')]) for number in range(10, 10 + count)}
        options = ReportOptions()
        p = policy(conceal({'section': 'properties'})).bind(a, b, options)
        assert p.match_counts == [2]
        report = prepare_report(p.apply(compare_snapshots(a, b, options), strict=True))
        assert report.exit_code == 1
        data = canonical_json({'header': report.header, 'events': report.events, 'footer': report.footer})
        assert b'TOPSECRET' not in data
        assert b'public changed name' in data
        reports.append((data, render_text(report)))
        artifacts.append(bundle_bytes(report, tmp_path / f'count-{count}'))
    assert reports[0] == reports[1]
    assert artifacts[0] == artifacts[1]


def test_whole_verbs_masks_checkpoint_total_verbs_before_identifiers_and_artifacts(tmp_path):
    reports, artifacts = [], []
    for count in (2, 5):
        a, b = anonymous_verb_pair()
        for source, programs in ((a, 1), (b, count)):
            obj = source.objects[900]
            obj.verbs = []
            for i in range(programs):
                verb = Verb(f'TOPSECRET_VERB_{i}', 1, 0, -1, obj.id)
                verb.code = [f'TOPSECRET_SOURCE_{i}']
                obj.verbs.append(verb)
            source.total_verbs = programs + 1
        b.objects[2].name = 'public changed name'
        options = ReportOptions(view='checkpoint')
        p = policy(conceal({'section': 'verbs'})).bind(a, b, options)
        report = prepare_report(p.apply(compare_snapshots(a, b, options), strict=True))
        assert report.exit_code == 1
        assert all(event['old_path'] != [{'type': 'field', 'name': 'total_verbs'}] for event in report.events)
        data = canonical_json({'header': report.header, 'events': report.events, 'footer': report.footer})
        assert b'TOPSECRET' not in data
        assert b'public changed name' in data
        reports.append((data, render_text(report)))
        artifacts.append(bundle_bytes(report, tmp_path / f'count-{count}'))
    assert reports[0] == reports[1]
    assert artifacts[0] == artifacts[1]


def report_artifacts(a, b, options, p, destination):
    comparison = compare_snapshots(a, b, options)
    assert comparison.exit_code != 2, comparison.footer
    report = prepare_report(p.apply(comparison, strict=True))
    return (canonical_json({'header': report.header, 'events': report.events, 'footer': report.footer}),
            render_text(report), bundle_bytes(report, destination))


@pytest.mark.parametrize('view', ['world', 'checkpoint'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('anonymous', [False, True])
def test_empty_whole_verbs_lifecycle_annotations_are_opaque(tmp_path, view, reverse, anonymous):
    artifacts = []
    for count in (0, 1):
        (a, b), _ = lifecycle_count_pair('permanent-verbs', count, graph=False)
        hidden = b.objects[3]
        for verb in hidden.verbs:
            verb.code = None
        b.total_verbs = 0
        if anonymous:
            del b.objects[3]
            hidden.id = 900
            hidden.anon = True
            for verb in hidden.verbs:
                verb.object = 900
            b.objects[900] = hidden
            b.total_objects -= 1
            for source, value in ((a, None), (b, Anon(900))):
                source.objects[2].properties = [Property('public root', value, 1, 0)]
                source.objects[2].propdefs_count = 1
        if reverse:
            a, b = b, a
        options = ReportOptions(view=view)
        p = policy(conceal({'section': 'verbs'})).bind(a, b, options)
        artifacts.append(report_artifacts(a, b, options, p, tmp_path / str(count)))
    assert artifacts[0] == artifacts[1]


@pytest.mark.parametrize('section', ['objects', 'heap', 'players', 'recycled', 'runtime', 'serialization'])
def test_whole_global_section_empty_selected_container_matches_presence(section):
    a = database()
    options = ReportOptions(view='checkpoint', sections=(section,), object_ids=())
    p = policy(conceal({'section': section})).bind(a, a, options)
    assert p.match_counts == [2]
    assert p.apply(compare_snapshots(a, a, options), strict=True).exit_code == 0


@pytest.mark.parametrize('section', ['objects', 'heap', 'players', 'recycled', 'runtime', 'serialization'])
def test_whole_global_section_excluded_container_has_no_match(section):
    a = database()
    options = ReportOptions(view='checkpoint', sections=('properties',), object_ids=(2,))
    assert policy(conceal({'section': section})).bind(a, a, options).match_counts == [0]


@pytest.mark.parametrize('object_ids,expected', [((2,), 2), ((), 0)])
def test_whole_objects_label_only_matching_uses_selected_owner_presence(object_ids, expected):
    a = database(MooObject(2, 'TOPSECRET_NAME', 0, 1, -1))
    options = ReportOptions(view='code', object_ids=object_ids)
    p = policy(conceal({'section': 'objects'})).bind(a, a, options)
    assert p.match_counts == [expected]


def test_whole_players_empty_to_positive_container_has_boolean_matching():
    a = database(MooObject(2, 'public', 0, 1, -1))
    b = deepcopy(a)
    b.players = [2]
    b.total_players = 1
    options = ReportOptions(view='checkpoint', object_ids=(2,))
    p = policy(conceal({'section': 'players'})).bind(a, b, options)
    assert p.match_counts == [2]
    assert p.apply(compare_snapshots(a, b, options), strict=True).exit_code == 1


@pytest.mark.parametrize('section,view', [('heap', 'world'), ('heap', 'checkpoint'),
    ('players', 'world'), ('players', 'checkpoint'), ('objects', 'checkpoint'), ('recycled', 'checkpoint')])
def test_whole_section_cardinality_and_declared_counts_are_opaque(tmp_path, section, view):
    artifacts = []
    for count in (2, 5):
        public = MooObject(2, 'public old', 0, 1, -1)
        other = MooObject(3, 'public selected', 0, 1, -1)
        a = database(public, other)
        b = deepcopy(a)
        b.objects[2].name = 'public changed name'
        object_ids = None
        if section == 'heap':
            for source in (a, b):
                source.objects[2].properties = [Property('public root', WaifReference(10), 1, 0)]
                source.objects[2].propdefs_count = 1
            a.waifs = {10: Waif(3, 1, [(0, 'TOPSECRET_OLD')])}
            b.waifs = {n: Waif(3, 1, [(0, WaifReference(n + 1) if n < 9 + count else 'TOPSECRET_NEW')])
                       for n in range(10, 10 + count)}
        elif section == 'players':
            # Hidden unselected membership also changes declared total_players.
            object_ids = (2, 3)
            for source in (a, b):
                source.objects.update({n: MooObject(n, f'public unselected {n}', 0, 1, -1) for n in range(10, 15)})
                source.total_objects = len(source.objects)
            a.players = [2]
            b.players = [3] + list(range(10, 10 + count))
            a.total_players, b.total_players = len(a.players), len(b.players)
        elif section == 'objects':
            b.objects.update({n: MooObject(n, f'TOPSECRET_{n}', 0, 1, -1) for n in range(10, 10 + count)})
            b.total_objects = len(b.objects)
        else:
            b.recycled_objects = list(range(10, 10 + count))
            b.total_objects += count
        options = ReportOptions(view=view, object_ids=object_ids)
        p = policy(conceal({'section': section})).bind(a, b, options)
        assert p.match_counts == [2]
        artifacts.append(report_artifacts(a, b, options, p, tmp_path / str(count)))
    assert artifacts[0] == artifacts[1]


@pytest.mark.parametrize('selector', [{'section': 'heap'},
    {'section': 'properties', 'definer': 2, 'name': 'secret'},
    {'section': 'verbs', 'object': 2, 'name': 'TOPSECRET_VERB'}])
def test_concealed_anonymous_programs_mask_declared_count(tmp_path, selector):
    artifacts = []
    for count in (2, 5):
        holder = MooObject(2, 'public old', 0, 1, -1)
        holder.properties = [Property('secret', Anon(900), 1, 0)]
        holder.propdefs_count = 1
        anon = MooObject(900, 'anonymous', 0, 1, -1, anon=True)
        a = database(holder, anon)
        b = deepcopy(a)
        b.objects[2].name = 'public changed name'
        for source, programs in ((a, 1), (b, count)):
            program_owner = source.objects[2 if selector['section'] == 'verbs' else 900]
            for i in range(programs):
                verb = Verb('TOPSECRET_VERB', 1, 0, -1, program_owner.id)
                verb.code = [f'TOPSECRET_SOURCE_{i}']
                program_owner.verbs.append(verb)
            source.total_verbs = programs
        options = ReportOptions(view='checkpoint')
        p = policy(conceal(selector)).bind(a, b, options)
        # Precise selectors retain actual matching-record cardinality, tested
        # separately. This regression isolates their associated count evidence.
        result = p.apply(compare_snapshots(a, b, options))
        assert all(event['old_path'] != [{'type': 'field', 'name': 'total_verbs'}] for event in result.events)
        if selector['section'] == 'verbs':
            continue
        artifacts.append(report_artifacts(a, b, options, p, tmp_path / str(count)))
    if artifacts:
        assert artifacts[0] == artifacts[1]


@pytest.mark.parametrize('view', ['world', 'checkpoint'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('section', ['players', 'recycled', 'objects', 'properties', 'verbs'])
def test_whole_section_disjoint_private_entity_ids_do_not_influence_artifacts(tmp_path, view, reverse, section):
    artifacts = []
    for variant, members in enumerate(((10, 11), (12, 13, 14))):
        public = MooObject(2, 'public unchanged name', 0, 1, -1)
        public.properties = [Property('public property', 'public old', 1, 0)]
        public.propdefs_count = 1
        a = database(public)
        if section in ('players', 'properties', 'verbs'):
            a.objects.update({n: MooObject(n, 'public unchanged holder', 0, 1, -1) for n in range(10, 15)})
            a.total_objects = len(a.objects)
        if section == 'properties':
            for obj in a.objects.values():
                if obj.id != 2:
                    obj.properties = [Property('TOPSECRET_PROPERTY', 'TOPSECRET_OLD', 1, 0)]
                    obj.propdefs_count = 1
        if section == 'verbs':
            for obj in a.objects.values():
                if obj.id != 2:
                    verb = Verb('TOPSECRET_VERB', 1, 0, -1, obj.id)
                    verb.code = ['TOPSECRET_OLD']
                    obj.verbs = [verb]
            a.total_verbs = 5
        b = deepcopy(a)
        if section == 'properties':
            b.objects[2].name = 'public changed name'
        else:
            b.objects[2].properties[0].value = 'public changed property'
        if section == 'players':
            b.players = list(members)
            b.total_players = len(members)
        elif section == 'recycled':
            b.recycled_objects = set(members)
            b.total_objects += len(members)
        elif section == 'objects':
            b.objects.update({n: MooObject(n, 'TOPSECRET_OBJECT', 0, 1, -1) for n in members})
            b.total_objects = len(b.objects)
        elif section == 'properties':
            for n in members:
                b.objects[n].properties[0].value = 'TOPSECRET_NEW'
        else:
            for n in members:
                b.objects[n].verbs[0].code = ['TOPSECRET_NEW']
        if reverse:
            a, b = b, a
        options = ReportOptions(view=view)
        p = policy(conceal({'section': section})).bind(a, b, options)
        comparison = compare_snapshots(a, b, options)
        assert comparison.exit_code == 1
        result = p.apply(comparison, strict=True)
        markers = [event for event in result.events if event['details'].get('concealed')]
        assert markers
        assert all(event['entity'] == {'type': 'global', 'id': 'database'} for event in markers)
        assert all(event['labels'] == {'old': None, 'new': None} for event in markers)
        assert not any(event['entity'].get('id') in ('10', '12') for event in result.events)
        report_data = report_artifacts(a, b, options, p, tmp_path / str(variant))
        assert b'public changed' in report_data[0]
        assert b'TOPSECRET' not in report_data[0]
        artifacts.append(report_data)
    assert artifacts[0] == artifacts[1]


def test_named_property_marker_retains_selected_public_entity_coordinate():
    obj = MooObject(2, 'public holder', 0, 1, -1)
    obj.properties = [Property('secret', 'TOPSECRET_OLD', 1, 0)]
    obj.propdefs_count = 1
    a = database(obj)
    b = deepcopy(a)
    b.objects[2].properties[0].value = 'TOPSECRET_NEW'
    p = policy(conceal({'section': 'properties', 'definer': 2, 'name': 'secret'})).bind(a, b)
    result = p.apply(compare_snapshots(a, b))
    assert len(result.events) == 1
    assert result.events[0]['entity'] == {'type': 'permanent', 'id': '2'}
    assert b'TOPSECRET' not in canonical_json(result.events)
