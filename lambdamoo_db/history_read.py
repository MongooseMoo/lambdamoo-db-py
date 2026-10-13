"""Bounded temporal queries over authenticated checkpoint evidence.

Permanent numbers describe slots, not proved object incarnations. This reader
preserves transitions and their uncertainty; it never joins edges into a route.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from .diff_history import validate_history
from .diff_report import _resolve, _terminal
from .diff_types import CATEGORIES, KINDS, SECTIONS, canonical_json

_LOWER = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not re.fullmatch(r'#?\d+', str(value), flags=re.ASCII):
        raise ValueError('History queries require a nonnegative permanent object number')
    return str(int(str(value).removeprefix('#')))


def _bound(value, name, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')


def _presence(value, target):
    """Inspect tagged values, following only reachable finite heap nodes.

    Integers, strings, code mentions, heap numbers and previews are not object
    references. Redaction makes absence unknown. Return presence and missing
    target evidence, without disclosing hidden values.
    """
    pending = [value]
    found = missing = concealed = False
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            kind = item.get('type')
            if kind == 'obj':
                if int(item['value']) == int(target):
                    found = True
                    missing |= item.get('missing_target') is True
            elif kind == 'redacted':
                concealed = True
            elif kind == 'graph':
                # The graph representation is finite even for cyclic heaps.
                table = {(n['kind'], n['id']): n['value'] for n in item['nodes']}
                reached = set()
                graph_pending = [item['root']]
                while graph_pending:
                    node = graph_pending.pop()
                    if isinstance(node, list):
                        graph_pending.extend(node)
                    elif isinstance(node, dict):
                        if node.get('type') in ('anon', 'waif'):
                            key = ('anonymous' if node['type'] == 'anon' else 'waif', node['value'])
                            if not (node['type'] == 'anon' and node['value'] == '-1') and (
                                    key not in table or table[key].get('type') == 'missing'):
                                concealed = True  # unavailable heap is not proof of absence
                            if key not in reached and key in table:
                                reached.add(key)
                                graph_pending.append(table[key])
                        graph_pending.extend(node.values())
                        if node.get('type') in ('obj', 'redacted'):
                            pending.append(node)
            else:
                pending.extend(item.values())
    return ('present' if found else 'unknown' if concealed else 'absent'), missing


def query_history(path, *, object_id=None, target=None, category=None, sections=(), kinds=(),
                  property_name=None, path_prefix=None, direction='forward', offset=0, limit=50,
                  max_scan_events=100000, max_scan_edges=1000):
    """Return a bounded page of transition events plus explicit coverage gaps.

    Full authentication is mandatory and independent of search bounds. Each
    pair is authenticated once and released after interrogation; no dumps are
    opened. Payloads remain locators in returned events. A target filter reads
    complete tagged payloads, includes concealed candidates as unknown, and
    reports presence on each side rather than inventing relationship deltas.
    """
    number, target = _number(object_id), _number(target)
    for value, name, minimum in ((offset, 'offset', 0), (limit, 'limit', 1),
                                 (max_scan_events, 'max_scan_events', 1),
                                 (max_scan_edges, 'max_scan_edges', 1)):
        _bound(value, name, minimum)
    if category is not None and category not in CATEGORIES:
        raise ValueError('Unknown history category')
    if any(s not in SECTIONS for s in sections) or any(k not in KINDS for k in kinds):
        raise ValueError('Unknown history section or kind')
    if direction not in ('forward', 'backward'):
        raise ValueError('Direction must be forward or backward')
    if property_name is not None and not isinstance(property_name, str):
        raise ValueError('Property name must be a string')
    if path_prefix is not None and (not isinstance(path_prefix, list) or
            any(not isinstance(step, dict) or not isinstance(step.get('type'), str) for step in path_prefix)):
        raise ValueError('Path prefix must be a JSON array of typed path steps')
    root = Path(path)
    ordered = []
    searched_ids = set()
    transitions = {}
    events = []
    matches = scanned = 0
    event_bound_hit = False
    gaps = []

    def plan(manifest, edges):
        nonlocal ordered, searched_ids
        # Use the exact authenticated edge index, not a pre-validation reread
        # that could select a different timeline if the directory changed.
        ordered = list(reversed(edges)) if direction == 'backward' else edges
        searched_ids = {e['edge_id'] for e in ordered[:max_scan_edges]}

    def gap(reason, edge=None, **detail):
        gaps.append({'reason': reason, **({'edge_id': edge['edge_id']} if edge else {}), **detail})

    def consume(edge, loaded):
        nonlocal matches, scanned, event_bound_hit
        pair_root, pair, rows, spans = loaded
        if edge['edge_id'] not in searched_ids or event_bound_hit:
            return
        header = pair['header']
        options = header['options']
        payload_cache = {}

        def resolve(value):
            if isinstance(value, dict):
                if value.get('type') == 'payload_ref':
                    key = (value['path'], value.get('representation'))
                    if key not in payload_cache:
                        payload_cache[key] = _resolve(value, pair_root, pair.get('payloads', {}))
                    return payload_cache[key]
                return {k: resolve(v) for k, v in value.items()}
            if isinstance(value, list):
                return [resolve(v) for v in value]
            return value
        selected = options.get('object_ids')
        excluded = number is not None and selected is not None and number not in selected
        if excluded:
            gap('object_outside_report_scope', edge)
        missing_sections = sorted(set(sections) - set(options['sections']))
        if missing_sections:
            gap('sections_outside_report_scope', edge, sections=missing_sections)
        if not sections and set(options['sections']) != set(SECTIONS):
            gap('selected_report_scope', edge, sections=options['sections'], view=options['view'])
        if selected is not None and number is None:
            gap('selected_object_scope', edge)
        if options.get('kinds'):
            gap('filtered_report_details', edge, kinds=options['kinds'])
        transition = {'edge_id': edge['edge_id'], 'old_commit': edge['old_commit'],
                      'new_commit': edge['new_commit'], 'status': edge['status'],
                      'sources': edge['sources'], 'lifecycle': [], 'matched_events': 0}
        transitions[edge['edge_id']] = transition
        if excluded:
            return
        candidates = [span[2] for span in spans.values()]
        if direction == 'backward':
            candidates.reverse()
        for event in candidates:
            if scanned >= max_scan_events:
                event_bound_hit = True
                return
            scanned += 1
            details = event.get('details', {})
            selector_unknown = bool(number is not None and details.get('concealed') and event['entity']['type'] == 'global')
            if number is not None and event['entity'] != {'type': 'permanent', 'id': number} and not selector_unknown:
                continue
            if details.get('subtype') == 'lifecycle':
                transition['lifecycle'].append({'event_id': event['event_id'], 'entity': event['entity'],
                                                'old_state': details['old_state'], 'new_state': details['new_state']})
            if category is not None and event['category'] != category:
                continue
            if sections and event['section'] not in sections or kinds and event['kind'] not in kinds:
                continue
            paths = resolve([event['old_path'], event['new_path']])
            concealed_path = any(step.get('type') == 'redacted' for p in paths for step in p)
            if property_name is not None and not any(
                    step.get('type') == 'property' and isinstance(step.get('name'), str) and
                    step['name'].translate(_LOWER) == property_name.translate(_LOWER)
                    for p in paths for step in p):
                if not concealed_path:
                    continue
                selector_unknown = True
            if path_prefix is not None and not any(
                    canonical_json(p[:len(path_prefix)]) == canonical_json(path_prefix) for p in paths):
                if not concealed_path:
                    continue
                selector_unknown = True
            if selector_unknown:
                gap('concealed_selector_evidence', edge, event_id=event['event_id'])
            reference = None
            if target is not None:
                reference = {}
                for side, p in zip(('old', 'new'), paths):
                    evidence = resolve(event[side])
                    # ObjNum map keys are relationship evidence too; a property
                    # definer number is provenance, not a stored reference.
                    keys = [step['key'] for step in p if step.get('type') == 'map-key']
                    state, missing = _presence([evidence, keys], target)
                    reference[side] = {'presence': state, 'missing_target': missing}
                    if missing:
                        gap('missing_reference_target', edge, event_id=event['event_id'], side=side)
                states = [reference[s]['presence'] for s in ('old', 'new')]
                if states == ['absent', 'absent']:
                    continue
                reference['match'] = 'confirmed' if 'present' in states else 'unknown'
                if 'unknown' in states:
                    gap('concealed_reference_evidence', edge, event_id=event['event_id'])
            matches += 1
            transition['matched_events'] += 1
            if offset < matches <= offset + limit:
                item = {'edge_id': edge['edge_id'], 'old_commit': edge['old_commit'],
                        'new_commit': edge['new_commit'], 'event': deepcopy(event),
                        'selector_match': 'unknown' if selector_unknown else 'confirmed'}
                if reference is not None:
                    item['reference'] = reference
                events.append(item)

    manifest = validate_history(root, _consume_pair=consume, _prepare_query=plan,
                                _reverse_pairs=direction == 'backward')
    for edge in ordered:
        if not edge.get('bundle'):
            gap('failed_edge', edge, old_commit=edge['old_commit'], new_commit=edge['new_commit'])
            if edge['edge_id'] in searched_ids:
                transitions[edge['edge_id']] = {'edge_id': edge['edge_id'], 'old_commit': edge['old_commit'],
                    'new_commit': edge['new_commit'], 'status': edge['status'], 'sources': None,
                    'lifecycle': [], 'matched_events': 0}
        for flag, reason in (('scan_complete', 'incomplete_comparison'), ('identity_complete', 'unknown_identity'),
                             ('details_complete', 'truncated_report_details'), ('payloads_complete', 'incomplete_payloads')):
            if not edge.get(flag):
                gap(reason, edge)
    if manifest['mode'] == 'endpoints' and manifest['old_commit'] != manifest['new_commit']:
        gap('intervening_checkpoints_unobserved')
    if manifest.get('requested_new_commit') != manifest['new_commit']:
        gap('requested_endpoint_unobserved', requested=manifest.get('requested_new_commit'), observed=manifest['new_commit'])
    if manifest.get('omitted_merge_parents'):
        gap('merge_parents_unobserved', commits=manifest['omitted_merge_parents'])
    if manifest.get('errors'):
        gap('history_build_incomplete')
    if len(ordered) > max_scan_edges:
        gap('query_edge_scan_limit', limit=max_scan_edges)
    if event_bound_hit:
        gap('query_event_scan_limit', limit=max_scan_events)
    query_complete = not event_bound_hit and len(ordered) <= max_scan_edges
    return {'schema_version': 1, 'report_type': 'history-query',
            'query': {'object_id': number, 'target': target, 'category': category, 'sections': list(sections),
                      'kinds': list(kinds), 'property': property_name, 'path_prefix': path_prefix,
                      'direction': direction, 'offset': offset, 'limit': limit,
                      'max_scan_events': max_scan_events, 'max_scan_edges': max_scan_edges},
            'old_commit': manifest['old_commit'], 'new_commit': manifest['new_commit'],
            'events': events, 'transitions': [transitions[e['edge_id']] for e in ordered if e['edge_id'] in transitions],
            'coverage': {'authenticated': True, 'query_scan_complete': query_complete,
                         'matched_events': matches, 'matched_events_exact': query_complete,
                         'scanned_events': scanned, 'scanned_edges': len(transitions),
                         'returned_events': len(events), 'page_complete': query_complete and matches <= offset + limit,
                         'next_offset': offset + len(events) if matches > offset + limit else None,
                         'continuous_identity': 'unknown', 'identity_basis': 'permanent numeric slot',
                         'gaps': gaps}}


def render_history_query(result):
    """Render locators and coverage only; property values and code stay in JSON."""
    lines = [f"History {result['old_commit']} -> {result['new_commit']}"]
    coverage = result['coverage']
    lines.append(f"{coverage['returned_events']} events returned; {coverage['matched_events']} matched; "
                 f"{coverage['scanned_events']} events searched; scan complete: {coverage['query_scan_complete']}")
    lines.append('Identity: permanent numeric slots; continuity unknown.')
    for item in result['events']:
        event = item['event']
        lines.append(f"{item['old_commit']} -> {item['new_commit']}  {event['entity']['type']}:{event['entity']['id']} "
                     f"{event['section']}/{event['category']} {event['kind']}  event {event['event_id']}  edge {item['edge_id']}")
        for side in ('old', 'new'):
            locators = []
            for step in event[side + '_path']:
                kind = step.get('type')
                if kind == 'property':
                    locators.append(f"property {step.get('name')} (definer #{step.get('definer')}, slot {step.get('slot')})")
                elif kind == 'field':
                    locators.append('field ' + str(step.get('name')))
                elif kind == 'map-key':
                    locators.append('map key type ' + str(step['key'].get('type')))
                else:
                    locators.append(str(kind))
            if locators:
                lines.append('  ' + side.upper() + ': ' + _terminal(' / '.join(locators)))
        if item['selector_match'] == 'unknown':
            lines.append('  selector match: unknown (concealed coordinates)')
        if item.get('reference'):
            ref = item['reference']
            lines.append(f"  reference: {ref['old']['presence']} -> {ref['new']['presence']} ({ref['match']})")
            for side in ('old', 'new'):
                if ref[side]['missing_target']:
                    lines.append('  ' + side.upper() + ': referenced target is absent from that checkpoint')
    for reason in sorted({g['reason'] for g in coverage['gaps']}):
        lines.append('Coverage gap: ' + reason)
    if coverage['next_offset'] is not None:
        lines.append(f"Next page: --offset {coverage['next_offset']}")
    return '\n'.join(lines)
