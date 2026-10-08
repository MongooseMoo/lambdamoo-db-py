"""Typed evidence and options shared by comparison and report consumers."""
from __future__ import annotations

from dataclasses import dataclass
import enum
import json
import struct
import heapq
from typing import Any

import attrs

from .database import CLEAR, Anon, MooCatch, MooError, MooFinally, ObjNum, WaifReference
from .map_keys import NUMERIC_MAP_KEY_POLICY

SECTIONS = ('objects', 'properties', 'verbs', 'heap', 'players', 'recycled', 'runtime', 'serialization')
CATEGORIES = ('code', 'definitions', 'values', 'heap', 'runtime', 'serialization')
KINDS = ('added', 'removed', 'changed', 'type_changed', 'order_changed', 'identity_conflict', 'identity_unresolved')
VIEW_SECTIONS = {'world': SECTIONS[:6], 'code': ('verbs',), 'checkpoint': SECTIONS}
POLICY_VERSION = 'typed-exact-anchored-v2:' + NUMERIC_MAP_KEY_POLICY


class _Sentinel:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name.upper()


MISSING = _Sentinel('missing')
UNSET = _Sentinel('unset')


@dataclass(frozen=True)
class ReportOptions:
    view: str = 'world'
    object_ids: tuple[int, ...] | None = None
    sections: tuple[str, ...] | None = None
    stop_after: int | None = None
    context: int = 3

    def __post_init__(self):
        if self.view not in VIEW_SECTIONS:
            raise ValueError(f'unknown view: {self.view}')
        if self.context < 0:
            raise ValueError('context must be nonnegative')
        if self.stop_after is not None and self.stop_after <= 0:
            raise ValueError('stop_after must be positive')
        if self.object_ids is not None:
            object.__setattr__(self, 'object_ids', tuple(sorted(set(int(i) for i in self.object_ids))))
        sections = VIEW_SECTIONS[self.view] if self.sections is None else self.sections
        if not sections or any(s not in VIEW_SECTIONS[self.view] for s in sections):
            raise ValueError(f'unsupported sections for {self.view}: {sections}')
        object.__setattr__(self, 'sections', tuple(s for s in SECTIONS if s in sections))

    def normalized(self):
        return {'view': self.view, 'object_ids': None if self.object_ids is None else [str(i) for i in self.object_ids],
                'sections': list(self.sections), 'stop_after': self.stop_after, 'context': self.context}


@dataclass
class Comparison:
    header: dict
    events: list[dict]
    footer: dict

    @property
    def exit_code(self):
        return {'equal': 0, 'different': 1, 'error': 2, 'unknown': 3}[self.footer['status']]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def value_kind(value: Any) -> str:
    """Classify supported parsed representation without allocating evidence."""
    if value is MISSING or value is UNSET:
        return value.name
    if value is CLEAR:
        return 'clear'
    if value is None:
        return 'none'
    for cls, tag in ((ObjNum, 'obj'), (Anon, 'anon'), (MooError, 'error'), (MooCatch, 'catch'), (MooFinally, 'finally'), (WaifReference, 'waif')):
        if isinstance(value, cls):
            return tag
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int):
        return 'int'
    if isinstance(value, float):
        return 'float'
    if isinstance(value, str):
        return 'string'
    for cls, tag in ((list, 'list'), (tuple, 'tuple'), (set, 'set'), (dict, 'map')):
        if isinstance(value, cls):
            return tag
    if attrs.has(type(value)):
        return 'record'
    raise ValueError(f'unsupported parsed value type: {type(value).__name__}')


def encode_value(value: Any, db=None) -> dict:
    """Encode parsed values losslessly; heap references use a finite per-side node table."""
    pending = []
    scheduled = set()
    nodes = []
    active = set()

    def schedule(reference):
        if reference not in scheduled:
            scheduled.add(reference)
            heapq.heappush(pending, reference)

    def encode(v):
        container = value_kind(v) in ('list', 'tuple', 'map', 'set', 'record')
        if container:
            identity = id(v)
            if identity in active:
                raise ValueError('direct Python container cycle is not a parsed heap reference')
            active.add(identity)
            try:
                return encode_inner(v)
            finally:
                active.remove(identity)
        return encode_inner(v)

    def encode_inner(v):
        if v is MISSING or v is UNSET:
            return {'type': v.name}
        if v is CLEAR:
            return {'type': 'clear'}
        if v is None:
            return {'type': 'none'}
        for cls, tag in ((ObjNum, 'obj'), (Anon, 'anon'), (MooError, 'error'), (MooCatch, 'catch'), (MooFinally, 'finally')):
            if isinstance(v, cls):
                result = {'type': tag, 'value': str(int(v))}
                if db is not None and cls is ObjNum and int(v) >= 0 and int(v) not in db.objects:
                    result['missing_target'] = True
                if db is not None and cls is Anon and int(v) != -1:
                    schedule(('anonymous', int(v)))
                return result
        if isinstance(v, WaifReference):
            if db is not None:
                schedule(('waif', v.index))
            return {'type': 'waif', 'value': str(v.index)}
        if isinstance(v, bool):
            return {'type': 'bool', 'value': v}
        if isinstance(v, (int, enum.IntFlag, enum.IntEnum)):
            return {'type': 'int', 'value': str(int(v))}
        if isinstance(v, float):
            return {'type': 'float', 'bits': struct.pack('>d', v).hex()}
        if isinstance(v, str):
            return {'type': 'string', 'value': v}
        if isinstance(v, (list, tuple)):
            return {'type': 'list' if isinstance(v, list) else 'tuple', 'items': [encode(x) for x in v]}
        if isinstance(v, set):
            return {'type': 'set', 'items': sorted((encode(x) for x in v), key=canonical_json)}
        if isinstance(v, dict):
            return {'type': 'map', 'entries': [{'key': encode(k), 'value': encode(x)} for k, x in v.items()]}
        if attrs.has(type(v)):
            return {'type': 'record', 'record_type': type(v).__name__,
                    'fields': {f.name: encode(getattr(v, f.name, UNSET)) for f in attrs.fields(type(v))}}
        raise ValueError(f'unsupported parsed value type: {type(v).__name__}')

    root = encode(value)
    while pending:
        kind, number = heapq.heappop(pending)
        node = db.waifs.get(number) if kind == 'waif' else db.objects.get(number)
        if kind == 'anonymous' and node is not None and not node.anon:
            node = None
        nodes.append({'kind': kind, 'id': str(number), 'value': encode(MISSING if node is None else node)})
    return {'type': 'graph', 'root': root, 'nodes': nodes} if nodes else root


def event_sort_key(event: dict):
    entity = event['entity']
    kind = entity['type']
    identity = (0, int(entity['id'])) if kind == 'permanent' else (1, entity['id'])
    return (CATEGORIES.index(event['category']), SECTIONS.index(event['section']),
            ('permanent', 'anonymous', 'waif', 'global').index(kind), identity,
            canonical_json(event.get('old_path', [])), canonical_json(event.get('new_path', [])),
            KINDS.index(event['kind']), canonical_json(event.get('details', {}).get('discriminator', None)))


def finalize_counts(comparison: Comparison):
    footer = comparison.footer
    footer['selected_changes'] = sum(bool(e['definite_change']) for e in comparison.events)
    footer['uncertainty_events'] = sum(e['kind'] in ('identity_conflict', 'identity_unresolved') for e in comparison.events)
    footer['changed_entities'] = len({(e['entity']['type'], e['entity']['id']) for e in comparison.events if e['definite_change']})
    footer['counts_by_category'] = {c: sum(e['category'] == c and e['definite_change'] for e in comparison.events) for c in CATEGORIES}
    footer['counts_by_kind'] = {k: sum(e['kind'] == k for e in comparison.events) for k in KINDS}
    footer['added_source_lines'] = sum(e.get('details', {}).get('added_lines', 0) for e in comparison.events)
    footer['removed_source_lines'] = sum(e.get('details', {}).get('removed_lines', 0) for e in comparison.events)
    # Callers apply kind/display limits after comparison or redaction. Never
    # preserve pre-redaction child counts when concealed roots coalesce.
    footer['displayed_events'] = len(comparison.events)
    footer['emitted_events'] = len(comparison.events)
    footer['omitted_events'] = 0 if footer.get('scan_complete') else None
    if not footer.get('scan_complete', True) or footer.get('errors'):
        footer['status'] = 'error'
    elif footer['selected_changes']:
        footer['status'] = 'different'
    elif not footer.get('identity_complete', True):
        footer['status'] = 'unknown'
    else:
        footer['status'] = 'equal'
    return comparison
