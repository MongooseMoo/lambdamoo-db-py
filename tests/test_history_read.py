"""Temporal evidence is authenticated, bounded and never stronger than its scope."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import Anon, MooDatabase, MooObject, ObjNum, Property, Waif, WaifReference
from lambdamoo_db.diff_history import build_history
from lambdamoo_db.diff_redaction import RedactionPolicy
from lambdamoo_db.diff_types import ReportOptions, canonical_json
from lambdamoo_db.history_read import query_history, render_history_query


def database(value):
    obj = MooObject(1, 'holder', 0, ObjNum(1), ObjNum(-1))
    obj.properties = [Property('wiring', value, ObjNum(1), 0)]
    obj.propdefs_count = 1
    db = MooDatabase(objects={1: obj, 2: MooObject(2, 'target', 0, ObjNum(1), ObjNum(-1))})
    db.version = 17
    db.versionstring = 'synthetic'
    db.total_objects = 2
    return db


@pytest.fixture
def history_factory(tmp_path, monkeypatch):
    count = 0

    def make(databases, *, broken=(), merges=False, **kwargs):
        nonlocal count
        count += 1
        commits = [f'{i+1:040x}' for i in range(len(databases))]
        class Repository:
            def __init__(self, _): pass
            def resolve(self, ref): return ref
            def pairs(self, start, end, mode='endpoints', **_):
                return list(zip(commits, commits[1:])) if mode == 'adjacent' else [(start, end)]
            def inventory(self, tip):
                return {'first_parent_tip': tip, 'shallow': False, 'branch_tips': []}
            def snapshot(self, ref, **_):
                index = commits.index(ref)
                if index in broken:
                    raise ValueError('Unavailable checkpoint')
                return SimpleNamespace(db=databases[index], metadata={
                    'kind': 'synthetic', 'sha256': hashlib.sha256(str(index).encode()).hexdigest(),
                    'bytes': index+1, 'format': 17, 'commit': ref})
            def commit_metadata(self, ref):
                index = commits.index(ref)
                parents = [commits[index-1]] if index else []
                if merges and index == len(commits)-1:
                    parents.append('f'*40)
                return {'commit': ref, 'parents': parents, 'checkpoint_time': None}
        monkeypatch.setattr('lambdamoo_db.diff_history.GitRepository', Repository)
        root = tmp_path / f'history-{count}'
        build_history('synthetic', commits[0], commits[-1], root, **kwargs)
        return root, commits
    return make


def reasons(result):
    return {g['reason'] for g in result['coverage']['gaps']}


def test_revert_has_two_distinct_provenanced_transitions(history_factory):
    root, commits = history_factory([database(ObjNum(2)), database(ObjNum(3)), database(ObjNum(2))], mode='adjacent')
    result = query_history(root, object_id='#1', target='#2', property_name='WIRING')
    assert [e['reference']['old']['presence'] for e in result['events']] == ['present', 'absent']
    assert [e['reference']['new']['presence'] for e in result['events']] == ['absent', 'present']
    assert [(e['old_commit'], e['new_commit']) for e in result['events']] == list(zip(commits, commits[1:]))
    assert len({e['event']['event_id'] for e in result['events']}) == 2
    assert result['coverage']['continuous_identity'] == 'unknown'
    assert result['coverage']['matched_events_exact']
    missing = query_history(root, object_id=1, target=3)
    assert missing['events'][0]['reference']['new']['missing_target']
    assert 'missing_reference_target' in reasons(missing)
    assert 'absent from that checkpoint' in render_history_query(missing)
    assert query_history(root, direction='backward')['events'][0]['new_commit'] == commits[-1]
    endpoints, _ = history_factory([database(ObjNum(2)), database(ObjNum(3)), database(ObjNum(2))])
    endpoint_query = query_history(endpoints, target=2)
    assert endpoint_query['events'] == []
    assert 'intervening_checkpoints_unobserved' in reasons(endpoint_query)


def test_pages_scan_limits_and_reverse_start_at_recent_transition(history_factory):
    root, commits = history_factory([database(ObjNum(2)), database(ObjNum(3)), database(ObjNum(2))], mode='adjacent')
    first = query_history(root, limit=1)
    second = query_history(root, limit=1, offset=first['coverage']['next_offset'])
    assert first['events'][0]['event']['event_id'] != second['events'][0]['event']['event_id']
    assert second['coverage']['next_offset'] is None
    bounded = query_history(root, direction='backward', max_scan_events=1)
    assert len(bounded['events']) == 1
    assert bounded['events'][0]['new_commit'] == commits[-1]
    assert not bounded['coverage']['matched_events_exact']
    assert 'query_event_scan_limit' in reasons(bounded)
    edge_bound = query_history(root, max_scan_edges=1)
    assert 'query_edge_scan_limit' in reasons(edge_bound)
    assert edge_bound['coverage']['scanned_edges'] == 1


def test_target_matches_typed_map_keys_and_not_numbers_or_code_strings(history_factory):
    old = database({ObjNum(2): 'old', 2: 'old', '#2': 'old'})
    new = database({ObjNum(2): 'new', 2: 'new', '#2': 'new'})
    root, _ = history_factory([old, new], mode='adjacent')
    result = query_history(root, target=2)
    assert len(result['events']) == 1
    event = result['events'][0]
    assert event['event']['old_path'][-1]['key'] == {'type': 'obj', 'value': '2'}
    assert event['reference']['old']['presence'] == event['reference']['new']['presence'] == 'present'
    prefix = event['event']['old_path'][:-1]
    assert len(query_history(root, path_prefix=prefix)['events']) == 3
    assert query_history(root, target=2, category='code')['events'] == []


def test_lifecycle_and_externalized_graph_target_closure(history_factory):
    old = MooDatabase()
    old.version = 17
    old.versionstring = 'synthetic'
    new = database(Anon(10))
    heap = MooObject(10, 'heap', 0, ObjNum(1), ObjNum(-1), anon=True)
    heap.properties = [Property('nested', [ObjNum(2), Anon(10), 'x'*20000], ObjNum(1), 0)]
    heap.propdefs_count = 1
    new.objects[10] = heap
    root, _ = history_factory([old, new, old], mode='adjacent')
    result = query_history(root, object_id=1, target=2)
    assert len(result['events']) == 2
    assert result['events'][0]['reference']['new']['presence'] == 'present'
    assert result['events'][1]['reference']['old']['presence'] == 'present'
    assert result['events'][0]['event']['new']['type'] == 'payload_ref'
    assert [t['lifecycle'][0]['new_state'] for t in result['transitions']] == ['present', 'absent']


def test_concealed_reference_evidence_is_unknown_without_value_or_path_leak(history_factory):
    policy = RedactionPolicy.from_data({'schema_version': 1, 'rules': [{
        'selector': {'section': 'properties', 'definer': 1, 'name': 'wiring', 'occurrence': '*'}, 'action': 'conceal'}]})
    old = database({'SECRET_MAP_KEY': ObjNum(2)})
    new = database({'SECRET_MAP_KEY': ObjNum(3)})
    root, _ = history_factory([old, new], redaction=policy, mode='adjacent')
    result = query_history(root, target=2)
    assert len(result['events']) == 1
    assert result['events'][0]['reference']['match'] == 'unknown'
    assert result['events'][0]['reference']['old']['presence'] == 'unknown'
    assert b'SECRET_MAP_KEY' not in canonical_json(result)
    assert 'concealed_reference_evidence' in reasons(result)
    filtered = query_history(root, object_id=1, target=2, property_name='wiring')
    assert filtered['events'][0]['selector_match'] == 'unknown'
    assert 'concealed_selector_evidence' in reasons(filtered)


def test_section_redaction_keeps_numeric_object_selector_unknown(history_factory):
    policy = RedactionPolicy.from_data({'schema_version': 1, 'rules': [{
        'selector': {'section': 'properties'}, 'action': 'conceal'}]})
    root, _ = history_factory([database(ObjNum(2)), database(ObjNum(3))], redaction=policy, mode='adjacent')
    result = query_history(root, object_id=1, target=2)
    assert result['events'][0]['event']['entity']['type'] == 'global'
    assert result['events'][0]['selector_match'] == 'unknown'
    assert b'wiring' not in canonical_json(result['events'])


def test_failed_edges_filters_and_unknown_identity_stay_explicit(history_factory):
    root, _ = history_factory([database(1), database(2), database(3)], mode='adjacent', broken=(1,), keep_going=True)
    assert 'failed_edge' in reasons(query_history(root))
    limited, _ = history_factory([database(1), database(2)], mode='adjacent', max_events=0)
    result = query_history(limited, target=2)
    assert result['events'] == []
    assert 'truncated_report_details' in reasons(result)
    scoped, _ = history_factory([database(1), database(2)], mode='adjacent',
                                options=ReportOptions(object_ids=(2,), sections=('objects',)))
    assert 'object_outside_report_scope' in reasons(query_history(scoped, object_id=1))
    filtered, _ = history_factory([database(1), database(2)], mode='adjacent', kinds=('added',))
    assert 'filtered_report_details' in reasons(query_history(filtered))
    unresolved_old = database(WaifReference(10))
    unresolved_old.waifs = {10: Waif(404, 1, [])}
    unresolved_new = deepcopy(unresolved_old)
    unresolved, _ = history_factory([unresolved_old, unresolved_new], mode='adjacent')
    assert 'unknown_identity' in reasons(query_history(unresolved))


def test_merge_parents_and_no_temporal_route_inference(history_factory):
    # A->B is removed before B->C is added. Their historical union is no route.
    first = database(ObjNum(2))
    second = database(ObjNum(-1))
    third = deepcopy(second)
    third.objects[2].properties = [Property('wiring', ObjNum(3), ObjNum(1), 0)]
    third.objects[2].propdefs_count = 1
    root, _ = history_factory([first, second, third], mode='adjacent', merges=True)
    result = query_history(root)
    assert len(result['events']) == 2
    assert result['events'][0]['event']['kind'] == 'changed'
    assert result['events'][1]['event']['kind'] == 'added'
    assert 'merge_parents_unobserved' in reasons(result)
    assert not any(k in result for k in ('routes', 'causes', 'continuous_graph'))


def test_each_pair_validates_once_even_many_events(history_factory, monkeypatch):
    import lambdamoo_db.diff_report as report
    old = database({str(i): i for i in range(30)})
    new = database({str(i): i+1 for i in range(30)})
    root, _ = history_factory([old, new, old], mode='adjacent')
    original = report._load_bundle
    calls = []
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(report, '_load_bundle', counted)
    result = query_history(root, limit=5)
    assert result['coverage']['matched_events'] == 60
    assert len(calls) == 2


def test_authentication_runs_past_search_limit_and_rejects_tampering(history_factory):
    root, _ = history_factory([database(1), database(2), database(3)], mode='adjacent')
    edges = [json.loads(line) for line in (root/'edges.jsonl').read_bytes().splitlines()]
    (root/edges[-1]['bundle']/'events.jsonl').write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        query_history(root, max_scan_edges=1)


def test_cli_text_is_safe_locators_json_is_explicit_evidence_and_limits_exit_two(history_factory):
    root, _ = history_factory([database('RAW_VALUE_OLD'), database('RAW_VALUE_NEW'), database(3)], mode='adjacent')
    runner = CliRunner()
    result = runner.invoke(moodb, ['history-read', str(root)], env={'MOODB': 'missing.db'})
    assert result.exit_code == 0, result.output
    assert 'RAW_VALUE' not in result.output
    assert 'property wiring' in result.output
    assert 'event ' in result.output and 'edge ' in result.output
    explicit = runner.invoke(moodb, ['history-read', str(root), '--format', 'json'])
    assert explicit.exit_code == 0
    assert json.loads(explicit.output)['events'][0]['event']['old']['value'] == 'RAW_VALUE_OLD'
    bounded = runner.invoke(moodb, ['history-read', str(root), '--max-scan-edges', '1'])
    assert bounded.exit_code == 2
    bad = runner.invoke(moodb, ['history-read', str(root), '--path-prefix', '{}'])
    assert bad.exit_code == 2


def test_default_text_does_not_print_map_key_strings_or_terminal_controls(history_factory):
    root, _ = history_factory([database({'SECRET_MAP_KEY': 1}), database({'SECRET_MAP_KEY': 2})], mode='adjacent')
    result = query_history(root)
    text = render_history_query(result)
    assert 'SECRET_MAP_KEY' not in text
    assert 'map key type string' in text
    for event in result['events']:
        event['event']['old_path'][0]['name'] = 'control\x1b[31m'
    result['old_commit'] = 'untrusted\x1b[31m'
    assert '\x1b' not in render_history_query(result)


@pytest.mark.parametrize('kwargs', [dict(object_id=True), dict(target=-1), dict(limit=0), dict(offset=True),
                                   dict(max_scan_events=0), dict(max_scan_edges=-1), dict(direction='up'),
                                   dict(category='other'), dict(path_prefix=[1])])
def test_invalid_query_parameters_fail_before_io(kwargs):
    with pytest.raises(ValueError):
        query_history('absent', **kwargs)


def test_linked_root_and_linked_artifact_rejected(history_factory, tmp_path):
    root, _ = history_factory([database(1), database(2)], mode='adjacent')
    linked = tmp_path/'linked'
    try:
        linked.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip('Host does not permit symlinks')
    with pytest.raises(ValueError, match='link'):
        query_history(linked)


def test_strict_history_json_rejects_duplicate_keys(history_factory):
    root, _ = history_factory([database(1), database(2)], mode='adjacent')
    data = (root/'manifest.json').read_text()
    (root/'manifest.json').write_text(data.replace('{', '{"schema_version":999,', 1))
    with pytest.raises(ValueError):
        query_history(root)
