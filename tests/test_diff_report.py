from copy import deepcopy
import hashlib
import json

import pytest

from lambdamoo_db.diff_report import (ReportError, iter_jsonl, prepare_report, read_bundle,
                                     render_text, validate_bundle, write_bundle)
from lambdamoo_db.diff_types import Comparison


def comparison(events=None):
    source = {'kind': 'file', 'label': 'synthetic.db', 'sha256': 'a' * 64, 'bytes': 12, 'format': 17}
    return Comparison({'record': 'header', 'schema_version': 1, 'tool_version': 'test', 'view': 'world',
        'policy_version': 'typed-exact-anchored-v1', 'sources': {'old': source, 'new': {**source, 'sha256': 'b' * 64}},
        'options': {'object_ids': None, 'sections': ['properties'], 'context': 3}, 'sections': ['properties'],
        'excluded_sections': ['runtime'], 'lineage_assumption': 'numeric slots'}, events or [],
        {'record': 'footer', 'scan_complete': True, 'identity_complete': True, 'details_complete': True,
         'payloads_complete': True, 'errors': [], 'excluded_sections': ['runtime']})


def event(n=1, old='old', new='new', *, category='values', kind='changed', subtype=None):
    result = {'record': 'event', 'entity': {'type': 'permanent', 'id': str(n)}, 'category': category,
        'section': 'verbs' if category == 'code' else 'properties', 'kind': kind, 'definite_change': True,
        'old_path': [{'type': 'property', 'slot': '0', 'name': 'name'}],
        'new_path': [{'type': 'property', 'slot': '0', 'name': 'name'}],
        'old': {'type': 'string', 'value': old}, 'new': {'type': 'string', 'value': new},
        'labels': {'old': 'label', 'new': 'label'}, 'details': {}}
    if subtype:
        result['details']['subtype'] = subtype
    return result


def test_ids_ignore_presentation_options_but_include_source_edge_and_selection():
    base = comparison([event()])
    full = prepare_report(base)
    filtered = prepare_report(base, kinds=['changed'], max_events=1)
    changed_context = deepcopy(base)
    changed_context.header['options']['context'] = 0
    assert full.events[0]['event_id'] == filtered.events[0]['event_id'] == prepare_report(changed_context).events[0]['event_id']
    for mutation in ('source', 'edge', 'selection'):
        other = deepcopy(base)
        if mutation == 'source':
            other.header['sources']['new']['sha256'] = 'c' * 64
        elif mutation == 'edge':
            other.header['edge'] = {'edge_id': 'd' * 64, 'old_commit': 'e' * 40, 'new_commit': 'f' * 40}
        else:
            other.header['options']['object_ids'] = ['1']
        assert prepare_report(other).events[0]['event_id'] != full.events[0]['event_id']
    assert 'event_id' not in base.events[0]


def test_filters_and_limits_preserve_selected_status():
    comp = prepare_report(comparison([event(), event(2, kind='added')]), kinds=['removed'], max_events=0)
    assert comp.events == [] and comp.footer['status'] == 'different'
    assert comp.footer['selected_changes'] == 2 and comp.footer['displayed_events'] == 0
    assert 'no events matching selected kinds' in render_text(comp)
    limited = prepare_report(comparison([event(), event(2)]), max_events=1)
    assert limited.footer['displayed_events'] == 2 and limited.footer['emitted_events'] == 1
    assert limited.footer['omitted_events'] == 1 and not limited.footer['details_complete']


def test_redaction_precedes_ids_counts_and_sorting():
    class Policy:
        def apply(self, comp, strict=False):
            assert strict
            comp.events = [event(old='opaque', new='opaque')]
            comp.header['redaction'] = {'policy_sha256': 'c' * 64}
            return comp
    comp = prepare_report(comparison([event(old='secret', new='secret1'), event(2)]), redaction=Policy(), redact_strict=True)
    assert comp.footer['selected_changes'] == 1
    assert 'secret' not in '\n'.join(iter_jsonl(comp))


def test_jsonl_inlines_exact_large_values_and_has_footer():
    value = 'é😀\r\n' * 4000
    comp = prepare_report(comparison([event(new=value)]))
    rows = [json.loads(line) for line in iter_jsonl(comp)]
    assert [row['record'] for row in rows] == ['header', 'event', 'footer']
    assert rows[1]['new']['value'] == value


def test_bundle_payload_integrity_unicode_offsets_and_range(tmp_path):
    value = 'é😀' * 5000
    comp = prepare_report(comparison([event(new=value), event(2, new='second')]))
    root = tmp_path / 'report'
    manifest = write_bundle(comp, root)
    assert manifest == validate_bundle(root)
    row = json.loads((root / 'objects.jsonl').read_text().splitlines()[0])
    for span in row['events']:
        data = (root / 'events.jsonl').read_bytes()[span['start']:span['end']]
        assert json.loads(data)['event_id'] == span['event_id']
    found = read_bundle(root, event=comp.events[0]['event_id'])
    assert found['new']['type'] == 'payload_ref'
    sliced = read_bundle(root, event=comp.events[0]['event_id'], side='new', value_range=(1, 4))
    assert sliced['value']['value'] == value[1:4] and sliced['unit'] == 'unicode-code-points'
    assert sliced['range'] == [1, 4]
    assert list((root / 'payloads').glob('*.json'))


def test_source_ranges_are_one_based_and_full_programs_are_available(tmp_path):
    e = event(category='code', subtype='source')
    e['old'] = {'type': 'list', 'items': [{'type': 'string', 'value': f'line{i}'} for i in range(1, 101)]}
    e['new'] = deepcopy(e['old']); e['new']['items'][50]['value'] = 'edited'
    comp = prepare_report(comparison([e]))
    root = tmp_path / 'report'; write_bundle(comp, root)
    result = read_bundle(root, event=comp.events[0]['event_id'], side='old', value_range=(1, 4))
    assert result['unit'] == 'source-lines'
    assert [x['value'] for x in result['value']['items']] == ['line1', 'line2', 'line3']
    assert '+edited' in render_text(comp, context=0)


def test_object_category_filter_happens_before_page_selection(tmp_path):
    comp = prepare_report(comparison([event(category='code'), event(), event(new='third')]))
    root = tmp_path / 'report'; write_bundle(comp, root)
    result = read_bundle(root, object_id='#1', category='values', offset=1, limit=1)
    assert len(result) == 1 and result[0]['new']['value'] == 'third'


@pytest.mark.parametrize('damage', ['footer', 'payload', 'index', 'completion', 'unsafe'])
def test_incomplete_or_corrupt_bundle_is_rejected(tmp_path, damage):
    root = tmp_path / 'report'; write_bundle(prepare_report(comparison([event(new='x' * 9000)])), root)
    if damage == 'footer':
        path = root / 'events.jsonl'; path.write_bytes(b'\n'.join(path.read_bytes().splitlines()[:-1]) + b'\n')
    elif damage == 'payload':
        next((root / 'payloads').glob('*.json')).write_bytes(b'changed')
    elif damage == 'index':
        path = root / 'objects.jsonl'; rows = [json.loads(x) for x in path.read_text().splitlines()]
        rows[0]['events'][0]['start'] += 1; path.write_text(json.dumps(rows[0]) + '\n')
    else:
        path = root / 'manifest.json'; data = json.loads(path.read_text())
        if damage == 'completion': data['complete'] = False
        else: data['files']['../outside'] = {'sha256': 'a' * 64, 'bytes': 1}
        path.write_text(json.dumps(data))
    with pytest.raises(ReportError): validate_bundle(root)


def test_existing_destination_is_preserved(tmp_path):
    root = tmp_path / 'report'; root.mkdir(); (root / 'user.txt').write_text('keep')
    with pytest.raises(ReportError): write_bundle(prepare_report(comparison()), root)
    assert (root / 'user.txt').read_text() == 'keep'


def test_terminal_and_markdown_escape_hostile_labels(tmp_path):
    e = event(new='```\x1b[31m\rsecret'); e['labels']['new'] = '[click](https://example.com)\n```'
    comp = prepare_report(comparison([e])); text = render_text(comp)
    assert '\x1b' not in text and '\r' not in text
    root = tmp_path / 'report'; write_bundle(comp, root)
    md = (root / 'objects' / 'permanent-1.md').read_text()
    assert '[click](https://example.com)' not in md and '\x1b' not in md


def test_entity_overview_and_detail_previews_are_bounded(tmp_path):
    events = [event(n) for n in range(1, 202)] + [event(1, new=str(i)) for i in range(60)]
    root = tmp_path / 'report'; manifest = write_bundle(prepare_report(comparison(events)), root)
    assert '1 more changed entities' in (root / 'summary.md').read_text()
    assert '11 more events' in (root / 'objects' / 'permanent-1.md').read_text()
    assert manifest['details_complete']
    assert len(read_bundle(root, object_id=1, limit=100)) == 61


def test_repreparation_restores_complete_events_before_new_limits():
    limited = prepare_report(comparison([event(), event(2)]), max_events=1)
    complete = prepare_report(limited)
    assert len(complete.events) == 2 and complete.footer['details_complete']


def test_bundle_refuses_nonatomic_destination_race(tmp_path, monkeypatch):
    import lambdamoo_db.diff_report as report
    root = tmp_path / 'report'
    real_publish = report._publish
    def racing_publish(source, destination):
        if str(destination) == str(root):
            root.mkdir(exist_ok=True)
            (root / 'user.txt').write_text('keep')
        return real_publish(source, destination)
    monkeypatch.setattr(report, '_publish', racing_publish)
    with pytest.raises(ReportError):
        write_bundle(prepare_report(comparison()), root)
    assert (root / 'user.txt').read_text() == 'keep'


def test_large_source_markdown_preview_is_bounded(tmp_path):
    e = event(category='code', subtype='source')
    e['old'] = {'type': 'list', 'items': []}
    e['new'] = {'type': 'list', 'items': [{'type': 'string', 'value': 'x' * 80} for _ in range(1000)]}
    root = tmp_path / 'report'; write_bundle(prepare_report(comparison([e])), root)
    assert (root / 'objects' / 'permanent-1.md').stat().st_size < 8000
    assert 'incomplete source preview' in (root / 'objects' / 'permanent-1.md').read_text()


def test_structural_index_checks_survive_recomputed_file_digest(tmp_path):
    root = tmp_path / 'report'; write_bundle(prepare_report(comparison([event()])), root)
    path = root / 'objects.jsonl'; row = json.loads(path.read_text())
    row['events'][0]['start'] += 1; path.write_text(json.dumps(row) + '\n')
    manifest_path = root / 'manifest.json'; manifest = json.loads(manifest_path.read_text())
    data = path.read_bytes(); manifest['files']['objects.jsonl'] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ReportError, match='span'): validate_bundle(root)


def test_invalid_ranges_and_missing_lookups_fail(tmp_path):
    root = tmp_path / 'report'; comp = prepare_report(comparison([event()])); write_bundle(comp, root)
    for kwargs in ({'event': 'absent'}, {'object_id': 999}, {'event': comp.events[0]['event_id'], 'side': 'old', 'value_range': (-1, 1)},
                   {'object_id': 1, 'offset': -1}, {'object_id': 1, 'category': 'bad'}):
        with pytest.raises(ReportError): read_bundle(root, **kwargs)


def test_real_comparison_and_heap_graph_payload_roundtrip(tmp_path):
    from lambdamoo_db.compare import compare_snapshots
    from lambdamoo_db.database import MooDatabase, MooObject, Property
    old = MooDatabase(); old.version = 17; old.versionstring = 'synthetic'
    old.total_objects = 1
    old.objects[1] = MooObject(1, 'obj', 0, 1, -1, [])
    old.objects[1].properties = [Property('p', 'old', 1, 0)]; old.objects[1].propdefs_count = 1
    new = deepcopy(old); new.objects[1].properties[0].value = 'new'
    raw = compare_snapshots(old, new); raw.header['sources'] = comparison().header['sources']
    comp = prepare_report(raw); root = tmp_path / 'report'; write_bundle(comp, root)
    assert read_bundle(root, object_id=1)[0]['new']['value'] == 'new'


def test_heap_namespaces_have_distinct_details_and_typed_lookup(tmp_path):
    events = [event(), event(new='waif')]
    events[0]['entity'] = {'type': 'anonymous', 'id': '1'}
    events[1]['entity'] = {'type': 'waif', 'id': '1'}
    for e in events: e['category'] = e['section'] = 'heap'
    root = tmp_path / 'report'; write_bundle(prepare_report(comparison(events)), root)
    rows = [json.loads(line) for line in (root / 'objects.jsonl').read_text().splitlines()]
    assert len({row['detail_filename'] for row in rows}) == 2
    assert read_bundle(root, object_id='waif:1')[0]['new']['value'] == 'waif'


def test_duplicate_json_keys_are_rejected(tmp_path):
    root = tmp_path / 'report'; write_bundle(prepare_report(comparison()), root)
    path = root / 'manifest.json'; data = path.read_text()
    path.write_text('{"complete":false,' + data[1:])
    with pytest.raises(ReportError): validate_bundle(root)


def test_added_verb_program_range_and_graph_collection_ranges(tmp_path):
    added = event(category='code', kind='added', subtype='verb_definition')
    added['old'] = {'type': 'missing'}
    added['new'] = {'type': 'record', 'record_type': 'Verb', 'fields': {'name': {'type': 'string', 'value': 'v'},
        'code': {'type': 'list', 'items': [{'type': 'string', 'value': 'one'}, {'type': 'string', 'value': 'two'}]}}}
    graph = event(2)
    graph['new'] = {'type': 'graph', 'root': {'type': 'list', 'items': [{'type': 'waif', 'value': '0'}]},
                    'nodes': [{'kind': 'waif', 'id': '0', 'value': {'type': 'record', 'record_type': 'Waif', 'fields': {}}}]}
    root = tmp_path / 'report'; comp = prepare_report(comparison([added, graph])); write_bundle(comp, root)
    source = read_bundle(root, event=comp.events[0]['event_id'], side='new', value_range=(1, 2))
    assert source['unit'] == 'source-lines' and source['value']['items'][0]['value'] == 'one'
    result = read_bundle(root, event=comp.events[1]['event_id'], side='new', value_range=(0, 1))
    assert result['value']['type'] == 'graph' and len(result['value']['nodes']) == 1


def test_actual_redaction_policy_conceals_all_artifacts_and_ranges(tmp_path):
    from lambdamoo_db.diff_redaction import RedactionPolicy
    from lambdamoo_db.compare import compare_snapshots
    from lambdamoo_db.database import MooDatabase, MooObject, Property
    old = MooDatabase(); old.version = 17; old.versionstring = 'synthetic'; old.total_objects = 1
    old.objects[1] = MooObject(1, 'obj', 0, 1, -1, [])
    old.objects[1].properties = [Property('p', {'secret-key': 'secret-value'}, 1, 0)]
    old.objects[1].propdefs_count = 1
    new = deepcopy(old); new.objects[1].properties[0].value = {'other-secret-key': 'new-secret-value'}
    raw = compare_snapshots(old, new); raw.header['sources'] = comparison().header['sources']
    policy = RedactionPolicy.from_data({'schema_version': 1, 'rules': [
        {'selector': {'section': 'properties', 'definer': '1', 'name': 'p'}, 'action': 'conceal'}]})
    policy.bind(old, new)
    comp = prepare_report(raw, redaction=policy, redact_strict=True)
    root = tmp_path / 'report'; write_bundle(comp, root)
    assert all(b'secret' not in path.read_bytes() for path in root.rglob('*') if path.is_file())
    result = read_bundle(root, event=comp.events[0]['event_id'], side='new', value_range=(0, 1))
    assert result['unit'] == 'concealed' and result['range'] is None
    assert result['value'] == {'type': 'redacted'}


def test_large_hunk_evidence_uses_tagged_payload_refs(tmp_path):
    e = event(category='code', subtype='source')
    e['details']['hunks'] = [{'old_range': [1, 2], 'new_range': [1, 2], 'opcodes': [
        {'tag': 'replace', 'old_lines': ['x' * 10000], 'new_lines': ['y' * 10000]}]}]
    root = tmp_path / 'report'; comp = prepare_report(comparison([e])); write_bundle(comp, root)
    found = read_bundle(root, event=comp.events[0]['event_id'])
    assert found['details']['hunks']['type'] == 'payload_ref'
    assert 'x' * 10000 not in (root / 'events.jsonl').read_text()


def test_jsonl_render_failure_still_emits_error_footer():
    comp = prepare_report(comparison([event()]))
    comp.events[0]['new'] = {'type': 'string', 'value': object()}
    rows = []
    with pytest.raises(ReportError):
        for line in iter_jsonl(comp): rows.append(json.loads(line))
    assert rows[-1]['record'] == 'footer' and rows[-1]['status'] == 'error'


@pytest.mark.parametrize('value', [{'type': 'int', 'value': 'not-an-int'}, {'type': 'bool', 'value': 1},
    {'type': 'float', 'bits': 'bad'}, {'type': 'list', 'items': ['untagged']}, {'type': 'map', 'entries': [{}]}])
def test_invalid_tagged_evidence_cannot_be_published(tmp_path, value):
    e = event(); e['new'] = value
    with pytest.raises(ReportError):
        write_bundle(prepare_report(comparison([e])), tmp_path / 'report')


def test_bundle_bytes_are_deterministic_across_destinations(tmp_path):
    comp = prepare_report(comparison([event(new='é' * 9000)]))
    first, second = tmp_path / 'one', tmp_path / 'two'
    write_bundle(comp, first); write_bundle(comp, second)
    assert {p.relative_to(first): p.read_bytes() for p in first.rglob('*') if p.is_file()} == {
            p.relative_to(second): p.read_bytes() for p in second.rglob('*') if p.is_file()}


def test_concealed_uncertainty_is_retained_in_public_counts():
    e = event(); e['details'] = {'concealed': True, 'concealed_uncertainty': True}
    e['old'] = e['new'] = {'type': 'redacted'}
    raw = comparison([e]); raw.footer['identity_complete'] = False
    comp = prepare_report(raw)
    assert comp.footer['selected_changes'] == 1
    assert comp.footer['uncertainty_events'] == 1 and comp.footer['status'] == 'different'


def test_event_identity_binds_declared_precision_policy():
    first = comparison([event()]); second = deepcopy(first)
    first.header['precision'] = {'float': 'binary64-exact'}
    second.header['precision'] = {'float': 'other-policy'}
    assert prepare_report(first).events[0]['event_id'] != prepare_report(second).events[0]['event_id']
