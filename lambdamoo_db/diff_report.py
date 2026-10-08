"""Deterministic report presentation, immutable bundles and bounded retrieval."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import difflib
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile

from .diff_types import (CATEGORIES, KINDS, SECTIONS, Comparison, canonical_json,
                         encode_value, event_sort_key, finalize_counts)
from .split import SplitError, _regular_path

PAYLOAD_LIMIT = 8192
PREVIEW_LIMIT = 1024
_VALUE_TYPES = {'int', 'obj', 'anon', 'waif', 'error', 'catch', 'finally', 'float', 'bool',
                'string', 'list', 'tuple', 'set', 'map', 'record', 'graph', 'clear', 'none', 'missing', 'unset'}
_HEX = re.compile(r'[0-9a-f]{64}')


class ReportError(ValueError):
    """A report is invalid, incomplete or cannot be safely published/read."""


def _identity_header(header):
    sources = header.get('sources', {})
    identities = {side: {key: source[key] for key in ('kind', 'sha256', 'bytes', 'format') if key in source}
                  for side, source in sources.items()}
    options = header.get('options', {})
    selectors = options.get('object_ids')
    selectors = None if selectors is None else [str(n) for n in sorted({int(n) for n in selectors})]
    result = {'schema_version': header.get('schema_version', 1), 'sources': identities,
              'view': header.get('view'), 'object_ids': selectors,
              'sections': [s for s in SECTIONS if s in header.get('sections', options.get('sections', []))],
              'policy_version': header.get('policy_version'), 'precision': header.get('precision'),
              'numeric_map_key_policy': header.get('numeric_map_key_policy')}
    if 'edge' in header:
        result['edge'] = {key: header['edge'].get(key) for key in ('edge_id', 'old_commit', 'new_commit')}
    if 'redaction' in header:
        # Policy metadata contains no values. Include only the declared digest,
        # rather than match counts, which are presentation evidence.
        redaction = header['redaction']
        result['redaction_policy'] = {key: value for key, value in redaction.items()
                                      if 'digest' in key or 'sha256' in key}
    return result


def _event_identity(event):
    return {key: event.get(key) for key in ('entity', 'old_entity', 'new_entity', 'category', 'section',
                                           'old_path', 'new_path', 'kind')} | {
        'discriminator': event.get('details', {}).get('discriminator'),
        'occurrence': event.get('occurrence', 0)}


def _event_id(header, event):
    return hashlib.sha256(canonical_json({'report': _identity_header(header), 'event': _event_identity(event)})).hexdigest()


def prepare_report(comparison, *, kinds=(), max_events=None, redaction=None, redact_strict=False):
    """Redact before assigning IDs, counting, filtering and limiting presentation."""
    if any(kind not in KINDS for kind in kinds):
        raise ReportError('unknown event kind')
    if max_events is not None and (not isinstance(max_events, int) or max_events < 0):
        raise ReportError('max_events must be nonnegative')
    comp = deepcopy(comparison)
    if hasattr(comp, '_unfiltered_events'):
        comp.events = deepcopy(comp._unfiltered_events)
        comp.footer = deepcopy(comp._unfiltered_footer)
    if redaction is not None:
        if comp.header.get('options', {}).get('stop_after') is not None:
            raise ReportError('stop_after cannot be combined with redaction')
        comp = redaction.apply(comp, strict=redact_strict)
    for event in comp.events:
        event.setdefault('old_entity', deepcopy(event['entity']))
        event.setdefault('new_entity', deepcopy(event['entity']))
        event.pop('event_id', None)
        event.pop('occurrence', None)
    def tie_key(event):
        evidence = deepcopy(event)
        evidence.get('details', {}).pop('hunks', None)
        return canonical_json(evidence)
    comp.events.sort(key=lambda e: (event_sort_key(e), tie_key(e)))
    occurrences = Counter()
    for event in comp.events:
        coordinate = canonical_json(_event_identity(event))
        event['occurrence'] = occurrences[coordinate]
        occurrences[coordinate] += 1
        event['event_id'] = _event_id(comp.header, event)
    finalize_counts(comp)
    comp.footer['uncertainty_events'] = sum(event['kind'] in ('identity_conflict', 'identity_unresolved') or
        event.get('details', {}).get('concealed_uncertainty', False) for event in comp.events)
    comp._unfiltered_events = deepcopy(comp.events)
    comp._unfiltered_footer = deepcopy(comp.footer)
    normalized_kinds = [kind for kind in KINDS if kind in kinds]
    comp.header.setdefault('options', {}).update({'kinds': normalized_kinds, 'max_events': max_events})
    matched = [event for event in comp.events if not kinds or event['kind'] in kinds]
    comp.events = matched if max_events is None else matched[:max_events]
    footer = comp.footer
    footer['displayed_events'] = len(matched)
    footer['emitted_events'] = len(comp.events)
    footer['omitted_events'] = len(matched) - len(comp.events) if footer.get('scan_complete') else None
    footer['details_complete'] = bool(footer.get('details_complete', True) and len(comp.events) == len(matched))
    footer.setdefault('payloads_complete', True)
    footer.setdefault('redacted_events', 0)
    footer.setdefault('redacted_values', 0)
    return comp


def iter_jsonl(comp):
    """Standalone JSON Lines always retains the complete emitted evidence inline."""
    emitted = 0
    try:
        for row in [comp.header, *comp.events, comp.footer]:
            encoded = canonical_json(row).decode('utf-8') + '\n'
            yield encoded
            emitted += row.get('record') == 'event'
    except (TypeError, ValueError, RecursionError, UnicodeError):
        footer = {'record': 'footer', 'status': 'error', 'scan_complete': False, 'identity_complete': False,
                  'details_complete': False, 'payloads_complete': False, 'selected_changes': comp.footer.get('selected_changes', 0),
                  'uncertainty_events': comp.footer.get('uncertainty_events', 0), 'displayed_events': comp.footer.get('displayed_events', 0),
                  'emitted_events': emitted, 'omitted_events': None, 'errors': [{'code': 'report_encoding_failed'}]}
        yield canonical_json(footer).decode('utf-8') + '\n'
        raise ReportError('cannot encode report evidence') from None


def _terminal(value):
    text = str(value)
    return ''.join(f'\\u{ord(c):04x}' if ord(c) < 32 or 127 <= ord(c) <= 159 or c in '\u2028\u2029\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069' else c for c in text)


def _markdown(value):
    text = html.escape(_terminal(value), quote=True)
    return re.sub(r'([\\`*_[\]{}()#+.!|~>-])', r'\\\1', text)


def _preview(value):
    data = canonical_json(value)
    if len(data) <= PREVIEW_LIMIT:
        return _terminal(data.decode('utf-8'))
    return _terminal(data[:PREVIEW_LIMIT].decode('utf-8', errors='ignore')) + ' … [incomplete preview]'


def _entity_key(entity):
    return entity['type'], str(entity['id'])


def _entity_label(entity):
    return '#' + str(entity['id']) if entity['type'] == 'permanent' else entity['type'] + ':' + str(entity['id'])


def _program(value):
    if value.get('type') == 'list' and all(item.get('type') == 'string' for item in value['items']):
        return [item['value'] for item in value['items']]
    return []


def _event_lines(event, *, context=3, markdown=False):
    escape = _markdown if markdown else _terminal
    heading = f"{_entity_label(event['entity'])} {event['category']} {event['kind']} {event['event_id']}"
    result = [escape(heading)]
    labels = event.get('labels', {})
    if any(label is not None for label in labels.values()):
        result.append('OLD label: ' + escape(labels.get('old')) + '; NEW label: ' + escape(labels.get('new')))
    result.extend(('OLD path: ' + escape(_preview(event.get('old_path', []))),
                   'NEW path: ' + escape(_preview(event.get('new_path', [])))))
    if event.get('details', {}).get('subtype') == 'source' and event['old'].get('type') != 'redacted' and event['new'].get('type') != 'redacted':
        old, new = _program(event['old']), _program(event['new'])
        result.append(f"OLD program: {event['old'].get('type')} ({len(old)} lines); NEW program: {event['new'].get('type')} ({len(new)} lines)")
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        source_bytes = 0
        truncated = False
        for group in matcher.get_grouped_opcodes(context):
            result.append(f"@@ OLD [{group[0][1]+1},{group[-1][2]+1}) NEW [{group[0][3]+1},{group[-1][4]+1}) @@")
            for tag, i, j, k, end in group:
                lines = [(' ', line) for line in old[i:j]] if tag == 'equal' else [('-', line) for line in old[i:j]] + [('+', line) for line in new[k:end]]
                for prefix, line in lines:
                    rendered = '    ' + escape(prefix + line)
                    source_bytes += len(rendered.encode())
                    if markdown and source_bytes > PREVIEW_LIMIT:
                        truncated = True
                        break
                    result.append(rendered)
                if truncated:
                    break
            if truncated:
                result.append(f"[incomplete source preview; moodb diff-read REPORT --event {event['event_id']} --side old --range START:END]")
                break
    else:
        result.extend(('OLD: ' + escape(_preview(event['old'])), 'NEW: ' + escape(_preview(event['new']))))
    if event.get('details'):
        result.append('Evidence: ' + escape(_preview(event['details'])))
    return result


def render_text(comp, context=3):
    if not isinstance(context, int) or context < 0:
        raise ReportError('context must be nonnegative')
    footer = comp.footer
    lines = [f"{footer['status']}: {footer['selected_changes']} selected changes; "
             f"{footer['uncertainty_events']} uncertainty events; {footer['displayed_events']} matching events; {footer['emitted_events']} emitted events",
             'Sources: ' + _preview(comp.header.get('sources', {})),
             'View: ' + _terminal(comp.header.get('view')), 'Filters: ' + _preview(comp.header.get('options', {})),
             'Lineage: ' + _terminal(comp.header.get('lineage_assumption', '')),
             'Excluded sections: ' + _preview(comp.header.get('excluded_sections', [])),
             'Redactions: ' + _preview(comp.header.get('redaction', {})),
             'Redaction rule matches: ' + _preview(footer.get('redaction_rules', []))]
    if not comp.events and footer.get('selected_changes') and comp.header.get('options', {}).get('kinds'):
        lines.append('no events matching selected kinds')
    for event in comp.events:
        lines.extend(['', *_event_lines(event, context=context)])
    lines.extend(['', 'Completeness: ' + _preview({key: footer.get(key) for key in
        ('scan_complete', 'identity_complete', 'details_complete', 'payloads_complete', 'omitted_events')}),
        'Counts by category: ' + _preview(footer.get('counts_by_category', {})),
        'Counts by kind: ' + _preview(footer.get('counts_by_kind', {}))])
    if footer.get('errors'):
        lines.append('Errors: ' + _preview(footer['errors']))
    return '\n'.join(lines) + '\n'


def _safe_name(name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name or name.startswith('/')
            or any(part in ('', '.', '..') for part in name.split('/'))
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ReportError('unsafe bundle path')
    return name


def _file(root, name):
    _safe_name(name)
    try:
        return _regular_path(root, name)
    except (SplitError, OSError):
        raise ReportError('bundle file is missing, nonregular or traverses a symlink') from None


def _detail_name(entity):
    kind, identity = _entity_key(entity)
    if kind == 'permanent':
        if not re.fullmatch(r'-?\d+', identity):
            raise ReportError('invalid permanent entity id')
        return f'objects/permanent-{int(identity)}.md'
    if kind in ('anonymous', 'waif'):
        safe = identity if re.fullmatch(r'[A-Za-z0-9_-]+', identity) else hashlib.sha256(identity.encode()).hexdigest()
        return f'objects/heap-{kind}-{safe}.md'
    return 'sections/global-' + hashlib.sha256(identity.encode()).hexdigest()[:16] + '.md'


def _externalize(value, staging, payloads, raw=False):
    if raw and isinstance(value, (str, list)) and len(canonical_json(value)) > PAYLOAD_LIMIT:
        reference = _externalize(encode_value(value), staging, payloads)
        reference['representation'] = 'raw'
        return reference
    if isinstance(value, dict):
        if value.get('type') in _VALUE_TYPES:
            data = canonical_json(value)
            if len(data) > PAYLOAD_LIMIT:
                digest = hashlib.sha256(data).hexdigest()
                name = f'payloads/sha256-{digest}.json'
                if name not in payloads:
                    _put(staging, name, data)
                    payloads[name] = {'sha256': digest, 'bytes': len(data)}
                return {'type': 'payload_ref', 'value_type': value['type'], 'encoded_bytes': len(data),
                        'sha256': digest, 'path': name,
                        'preview': {'text': data[:PREVIEW_LIMIT].decode('utf-8', errors='ignore'), 'complete': False}}
        return {key: _externalize(item, staging, payloads, raw=raw or key == 'details') for key, item in value.items()}
    if isinstance(value, list):
        return [_externalize(item, staging, payloads, raw=raw) for item in value]
    return value


def _put(root, name, data):
    path = root / _safe_name(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _summary(comp, rows):
    footer = comp.footer
    text = ['# Database comparison', '', _markdown(f"Status: {footer['status']}; selected changes: {footer['selected_changes']}; uncertainty: {footer['uncertainty_events']}"),
            '', 'Sources: ' + _markdown(canonical_json(comp.header.get('sources', {})).decode()),
            '', 'View: ' + _markdown(comp.header.get('view')), '', 'Filters: ' + _markdown(_preview(comp.header.get('options', {}))),
            '', 'Lineage: ' + _markdown(comp.header.get('lineage_assumption', '')), '',
            'Excluded sections: ' + _markdown(_preview(comp.header.get('excluded_sections', []))), '',
            'Counts by category: ' + _markdown(_preview(footer.get('counts_by_category', {}))), '',
            'Counts by kind: ' + _markdown(_preview(footer.get('counts_by_kind', {}))), '',
            'Redactions: ' + _markdown(_preview(comp.header.get('redaction', {}))), '',
            'Redaction rule matches: ' + _markdown(_preview(footer.get('redaction_rules', []))), '',
            'Completeness and ambiguities: ' + _markdown(_preview({key: footer.get(key) for key in
                ('scan_complete', 'identity_complete', 'details_complete', 'payloads_complete', 'redacted_events', 'redacted_values', 'errors')})),
            '', '[Full object index](objects.jsonl)', '']
    for row in rows[:200]:
        text.append(f"- [{_markdown(_entity_label(row['entity']))}]({row['detail_filename']}) "
                    + _markdown(_preview(row['labels'])) + ' ' + _markdown(_preview(row['counts_by_category'])))
    if len(rows) > 200:
        text.extend(['', f'{len(rows)-200} more changed entities; use objects.jsonl and moodb diff-read --object ID.'])
    return '\n'.join(text) + '\n'


def write_bundle(comp, path):
    """Publish a complete staged directory with an atomic, final manifest."""
    destination = Path(path).absolute()
    if destination.exists() or destination.is_symlink():
        raise ReportError('bundle destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.moodb-report-', dir=destination.parent))
    staging_target = staging.resolve()
    staging_identity = staging.stat().st_dev, staging.stat().st_ino
    try:
        payloads = {}
        events = [_externalize(event, staging, payloads) for event in comp.events]
        chunks = [canonical_json(comp.header) + b'\n']
        spans = defaultdict(list)
        position = len(chunks[0])
        for event in events:
            data = canonical_json(event) + b'\n'
            spans[_entity_key(event['entity'])].append({'event_id': event['event_id'], 'category': event['category'],
                                                       'start': position, 'end': position + len(data)})
            chunks.append(data); position += len(data)
        chunks.append(canonical_json(comp.footer) + b'\n')
        _put(staging, 'events.jsonl', b''.join(chunks))
        grouped = defaultdict(list)
        for event in comp.events:
            grouped[_entity_key(event['entity'])].append(event)
        rows = []
        for key, entity_events in grouped.items():
            entity = entity_events[0]['entity']
            filename = _detail_name(entity)
            row = {'entity': entity, 'labels': entity_events[0].get('labels', {}), 'detail_filename': filename,
                   'counts_by_category': dict(Counter(e['category'] for e in entity_events)), 'events': spans[key]}
            rows.append(row)
            detail = ['# ' + _markdown(_entity_label(entity)), '', 'OLD/NEW labels: ' + _markdown(_preview(row['labels'])), '']
            for event in entity_events[:50]:
                detail.extend(_event_lines(event, context=comp.header.get('options', {}).get('context', 3), markdown=True))
                detail.append('')
            if len(entity_events) > 50:
                detail.extend([f'{len(entity_events)-50} more events.', '',
                    f"moodb diff-read REPORT --object {_entity_label(entity)} --offset 50 --limit 50", ''])
            _put(staging, filename, ('\n'.join(detail) + '\n').encode())
        rows.sort(key=lambda row: (min(CATEGORIES.index(category) for category in row['counts_by_category']),
                                  ('permanent', 'anonymous', 'waif', 'global').index(row['entity']['type']),
                                  (0, int(row['entity']['id'])) if row['entity']['type'] == 'permanent' else (1, row['entity']['id'])))
        # Runtime and serialization remain discoverable by section even if a
        # global entity has events in both sections.
        for section in ('runtime', 'serialization'):
            selected = [event for event in comp.events if event['section'] == section]
            if selected:
                lines = ['# ' + section, '']
                for event in selected[:50]:
                    lines.extend(_event_lines(event, markdown=True)); lines.append('')
                if len(selected) > 50:
                    lines.append(f'{len(selected)-50} more events; use the object index.')
                _put(staging, f'sections/{section}.md', ('\n'.join(lines) + '\n').encode())
        _put(staging, 'objects.jsonl', b''.join(canonical_json(row) + b'\n' for row in rows))
        _put(staging, 'summary.md', _summary(comp, rows).encode())
        files = {}
        for child in sorted(staging.rglob('*')):
            if child.is_file():
                data = child.read_bytes()
                files[child.relative_to(staging).as_posix()] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        manifest = {'schema_version': 1, 'complete': True, 'header': deepcopy(comp.header), 'footer': deepcopy(comp.footer),
                    **deepcopy(comp.footer), 'files': files, 'payloads': payloads, 'object_index': 'objects.jsonl',
                    'events': 'events.jsonl', 'summary': 'summary.md'}
        temporary = staging / 'manifest.json.tmp'
        temporary.write_bytes(canonical_json(manifest) + b'\n')
        os.replace(temporary, staging / 'manifest.json')
        validate_bundle(staging)
        # rename refuses an existing directory on Windows. On POSIX an empty
        # user directory can otherwise be replaced: explicitly check again.
        if destination.exists() or destination.is_symlink():
            raise ReportError('bundle destination already exists')
        _publish(staging, destination)
        return manifest
    except OSError:
        raise ReportError('cannot publish report bundle') from None
    finally:
        if staging_target.exists() and not staging_target.is_symlink():
            status = staging_target.stat()
            if (status.st_dev, status.st_ino) == staging_identity and staging_target.name.startswith('.moodb-report-'):
                shutil.rmtree(staging_target)


def _publish(staging, destination):
    if os.name == 'nt':
        # Windows rename fails atomically when any destination already exists.
        os.rename(staging, destination)
        return
    # Portable POSIX publication reserves the destination exclusively, then
    # installs its completion marker atomically with an exclusive hard link.
    # A failure leaves an unfinished directory, never an equality claim.
    destination.mkdir()
    for file in sorted(staging.rglob('*')):
        if file.is_dir():
            (destination / file.relative_to(staging)).mkdir(parents=True, exist_ok=True)
            continue
        if not file.is_file() or file == staging / 'manifest.json':
            continue
        target = destination / file.relative_to(staging)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(file.read_bytes())
    os.link(staging / 'manifest.json', destination / 'manifest.json')


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReportError('duplicate bundle JSON key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ReportError('nonfinite JSON number')
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError):
        raise ReportError('invalid bundle JSON') from None


def _resolve(value, root, inventory=None, active=None):
    active = set() if active is None else active
    if isinstance(value, dict):
        if value.get('type') == 'payload_ref':
            name = value.get('path')
            digest = value.get('sha256')
            if (not isinstance(digest, str) or not _HEX.fullmatch(digest)
                    or name != f'payloads/sha256-{digest}.json'
                    or inventory is not None and name not in inventory or name in active):
                raise ReportError('invalid, undeclared or cyclic payload reference')
            data = _file(root, name).read_bytes()
            if len(data) != value.get('encoded_bytes') or hashlib.sha256(data).hexdigest() != value.get('sha256'):
                raise ReportError('payload digest or size mismatch')
            decoded = _json(data)
            if decoded.get('type') not in _VALUE_TYPES or decoded.get('type') != value.get('value_type'):
                raise ReportError('payload type mismatch')
            decoded = _resolve(decoded, root, inventory, active | {name})
            return _raw_value(decoded) if value.get('representation') == 'raw' else decoded
        return {key: _resolve(item, root, inventory, active) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, root, inventory, active) for item in value]
    return value


def _raw_value(value):
    kind = value['type']
    if kind in ('list', 'tuple', 'set'):
        return [_raw_value(item) for item in value['items']]
    if kind == 'map':
        return {_raw_value(entry['key']): _raw_value(entry['value']) for entry in value['entries']}
    if kind == 'int':
        return int(value['value'])
    if kind == 'float':
        return struct.unpack('>d', bytes.fromhex(value['bits']))[0]
    if kind == 'none':
        return None
    if kind in ('string', 'bool'):
        return value['value']
    raise ReportError('unsupported raw evidence payload')


def _check_value(value, payload_types=None):
    """Check tagged evidence independently of its serialization digest."""
    if not isinstance(value, dict):
        raise ReportError('untagged payload value')
    kind = value.get('type')
    if kind == 'payload_ref':
        if payload_types is None or payload_types.get(value.get('path')) != value.get('value_type') or value.get('representation') == 'raw':
            raise ReportError('invalid typed payload reference')
    elif kind in ('none', 'missing', 'unset', 'clear', 'redacted'):
        return
    elif kind in ('int', 'obj', 'anon', 'waif', 'error', 'catch', 'finally'):
        if not isinstance(value.get('value'), str) or not re.fullmatch(r'-?\d+', value['value'], flags=re.ASCII):
            raise ReportError('invalid tagged integer')
    elif kind == 'bool':
        if type(value.get('value')) is not bool:
            raise ReportError('invalid tagged Boolean')
    elif kind == 'float':
        if not isinstance(value.get('bits'), str) or not re.fullmatch(r'[0-9a-f]{16}', value['bits']):
            raise ReportError('invalid binary64 encoding')
    elif kind == 'string':
        if not isinstance(value.get('value'), str):
            raise ReportError('invalid tagged string')
    elif kind in ('list', 'tuple', 'set'):
        if not isinstance(value.get('items'), list):
            raise ReportError('invalid tagged collection')
        for item in value['items']: _check_value(item, payload_types)
    elif kind == 'map':
        if not isinstance(value.get('entries'), list):
            raise ReportError('invalid tagged map')
        for entry in value['entries']:
            if not isinstance(entry, dict) or not {'key', 'value'} <= entry.keys():
                raise ReportError('invalid tagged map entry')
            _check_value(entry['key'], payload_types); _check_value(entry['value'], payload_types)
    elif kind == 'record':
        if not isinstance(value.get('record_type'), str) or not isinstance(value.get('fields'), dict):
            raise ReportError('invalid tagged record')
        for item in value['fields'].values(): _check_value(item, payload_types)
    elif kind == 'graph':
        _check_value(value['root'], payload_types)
        if not isinstance(value.get('nodes'), list):
            raise ReportError('invalid finite graph')
        identities = set()
        for node in value['nodes']:
            identity = node.get('kind'), node.get('id')
            if identity in identities or identity[0] not in ('anonymous', 'waif') or not isinstance(identity[1], str) or not re.fullmatch(r'-?\d+', identity[1], flags=re.ASCII):
                raise ReportError('invalid or duplicate finite graph node')
            identities.add(identity)
            _check_value(node['value'], payload_types)
    else:
        raise ReportError('unknown tagged payload kind')


def _load_bundle(path):
    root = Path(path)
    manifest = _json(_file(root, 'manifest.json').read_bytes())
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or manifest.get('complete') is not True:
        raise ReportError('unfinished or unsupported bundle manifest')
    files = manifest.get('files')
    if not isinstance(files, dict) or not {'events.jsonl', 'objects.jsonl', 'summary.md'} <= files.keys():
        raise ReportError('bundle file inventory is incomplete')
    # Validate every path before reading any inventory entry.
    paths = {name: _file(root, name) for name in files}
    for name, file in paths.items():
        data = file.read_bytes(); expected = files[name]
        if len(data) != expected.get('bytes') or hashlib.sha256(data).hexdigest() != expected.get('sha256'):
            raise ReportError('bundle file digest or size mismatch')
    payloads = manifest.get('payloads', {})
    if not isinstance(payloads, dict):
        raise ReportError('invalid payload inventory')
    payload_types = {}
    for name, metadata in payloads.items():
        if name not in files or files[name] != metadata or name != f"payloads/sha256-{metadata.get('sha256')}.json":
            raise ReportError('payload inventory mismatch')
        payload = _json(paths[name].read_bytes())
        if not isinstance(payload, dict) or payload.get('type') not in _VALUE_TYPES:
            raise ReportError('invalid tagged payload')
        _check_value(payload)
        payload_types[name] = payload['type']
    stream = paths['events.jsonl'].read_bytes()
    lines = stream.splitlines(keepends=True)
    if len(lines) < 2 or any(not line.endswith(b'\n') for line in lines):
        raise ReportError('bundle event stream has no complete footer')
    records = [_json(line) for line in lines]
    if records[0].get('record') != 'header' or records[-1].get('record') != 'footer' or any(record.get('record') != 'event' for record in records[1:-1]):
        raise ReportError('invalid bundle record sequence')
    if records[0] != manifest.get('header') or records[-1] != manifest.get('footer'):
        raise ReportError('manifest and event stream disagree')
    if any(manifest.get(key) != value for key, value in records[-1].items()):
        raise ReportError('manifest completeness metadata disagrees')
    events = records[1:-1]
    if records[-1].get('emitted_events') != len(events):
        raise ReportError('emitted event count mismatch')
    footer = records[-1]
    if any(type(footer.get(key)) is not bool for key in ('scan_complete', 'identity_complete', 'details_complete', 'payloads_complete')):
        raise ReportError('invalid completeness metadata')
    if (type(footer.get('selected_changes')) is not int or footer['selected_changes'] < sum(bool(e.get('definite_change')) for e in events)
            or type(footer.get('displayed_events')) is not int or footer['displayed_events'] < len(events)):
        raise ReportError('invalid report counts')
    status = ('error' if not footer['scan_complete'] or footer.get('errors') else 'different' if footer['selected_changes']
              else 'unknown' if not footer['identity_complete'] else 'equal')
    if footer.get('status') != status:
        raise ReportError('status contradicts completeness and selected changes')
    if footer['scan_complete']:
        if footer.get('omitted_events') != footer['displayed_events'] - len(events):
            raise ReportError('omitted event count mismatch')
    elif footer.get('omitted_events') is not None:
        raise ReportError('stopped report claims a known omitted count')
    if footer['details_complete'] and footer['displayed_events'] != len(events):
        raise ReportError('presentation-limited report claims complete details')
    event_spans = {}; offset = len(lines[0])
    previous_order = None
    for event, line in zip(events, lines[1:-1]):
        identity = event.get('event_id')
        if not isinstance(identity, str) or not _HEX.fullmatch(identity) or identity in event_spans:
            raise ReportError('invalid or duplicate event id')
        if event.get('kind') not in KINDS or event.get('category') not in CATEGORIES or event.get('section') not in SECTIONS or type(event.get('definite_change')) is not bool:
            raise ReportError('invalid event schema')
        _check_value(event['old'], payload_types); _check_value(event['new'], payload_types)
        def check_refs(value):
            if isinstance(value, dict):
                if value.get('type') == 'payload_ref':
                    name = value.get('path'); metadata = payloads.get(name)
                    preview = value.get('preview', {})
                    if (metadata is None or metadata['sha256'] != value.get('sha256') or metadata['bytes'] != value.get('encoded_bytes')
                            or payload_types.get(name) != value.get('value_type') or preview.get('complete') is not False
                            or not isinstance(preview.get('text'), str) or len(preview['text'].encode()) > PREVIEW_LIMIT):
                        raise ReportError('invalid or incomplete payload reference')
                else:
                    for item in value.values(): check_refs(item)
            elif isinstance(value, list):
                for item in value: check_refs(item)
        check_refs(event)
        coordinates = _resolve(_event_identity(event), root, payloads)
        coordinates['details'] = {'discriminator': coordinates['discriminator']}
        if _event_id(records[0], coordinates) != identity:
            raise ReportError('event identity does not match report sources and coordinates')
        order = event_sort_key(_resolve({key: event[key] for key in ('entity', 'category', 'section', 'old_path', 'new_path', 'kind', 'details')}, root, payloads))
        if previous_order is not None and order < previous_order:
            raise ReportError('event order is noncanonical')
        previous_order = order
        event_spans[identity] = (offset, offset + len(line), event)
        offset += len(line)
    rows = [_json(line) for line in paths['objects.jsonl'].read_bytes().splitlines()]
    indexed = set(); entities = set()
    for row in rows:
        key = _entity_key(row['entity'])
        if key in entities:
            raise ReportError('duplicate object index entity')
        entities.add(key)
        detail = row.get('detail_filename')
        if detail not in files:
            raise ReportError('object detail file is absent from inventory')
        for span in row.get('events', []):
            identity = span.get('event_id')
            if identity not in event_spans or identity in indexed:
                raise ReportError('index event id is missing or duplicated')
            start, end, event = event_spans[identity]
            if (span.get('start'), span.get('end')) != (start, end) or _entity_key(event['entity']) != key or span.get('category') != event['category']:
                raise ReportError('index byte span or event identity mismatch')
            indexed.add(identity)
    if indexed != set(event_spans):
        raise ReportError('object index omits event evidence')
    return root, manifest, rows, event_spans


def validate_bundle(path):
    try:
        return _load_bundle(path)[1]
    except (KeyError, TypeError, AttributeError, OSError, RecursionError):
        raise ReportError('invalid or incomplete bundle') from None


def _selector(value, rows):
    if isinstance(value, dict):
        return _entity_key(value)
    text = str(value)
    if re.fullmatch(r'#?-?\d+', text):
        return 'permanent', str(int(text.removeprefix('#')))
    matches = []
    for row in rows:
        entity = row['entity']
        if entity['type'] in ('anonymous', 'waif') and text in (entity['id'], _entity_label(entity)):
            matches.append(_entity_key(entity))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ReportError('heap id needs an explicit typed selector')
    raise ReportError('unknown typed object selector')


def _graph_nodes(root, nodes):
    """Retain exactly the finite node closure reached from sliced entries."""
    table = {(node['kind'], node['id']): node for node in nodes}
    pending = []
    def references(value):
        if isinstance(value, dict):
            if value.get('type') in ('anon', 'waif') and 'value' in value:
                if value.get('type') == 'anon' and value['value'] == '-1':
                    return
                pending.append(('anonymous' if value['type'] == 'anon' else 'waif', value['value']))
            for child in value.values(): references(child)
        elif isinstance(value, list):
            for child in value: references(child)
    references(root)
    reached = set()
    while pending:
        key = pending.pop()
        if key in reached:
            continue
        reached.add(key)
        if key in table:
            references(table[key]['value'])
    return [node for node in nodes if (node['kind'], node['id']) in reached]


def read_bundle(path, *, event=None, object_id=None, category=None, offset=0, limit=50,
                side=None, value_range=None):
    if offset < 0 or limit < 0 or category is not None and category not in CATEGORIES:
        raise ReportError('invalid pagination or category')
    if event is not None and object_id is not None or event is None and object_id is None:
        raise ReportError('select exactly one event or object')
    if side is not None and side not in ('old', 'new') or (value_range is not None) != (side is not None):
        raise ReportError('payload retrieval requires a side and bounded range')
    if value_range is not None and event is None:
        raise ReportError('payload retrieval requires an event')
    try:
        root, manifest, rows, spans = _load_bundle(path)
        if event is not None:
            if event not in spans:
                raise ReportError('event is absent from bundle')
            selected = deepcopy(spans[event][2])
            if value_range is None:
                return selected
            start, end = value_range
            value = _resolve(selected[side], root, manifest.get('payloads', {}))
            if value.get('type') == 'redacted':
                return {'event_id': event, 'side': side, 'value': value, 'unit': 'concealed', 'range': None}
            graph = value if value.get('type') == 'graph' else None
            value = value['root'] if graph else value
            subtype = selected.get('details', {}).get('subtype')
            source = subtype == 'source'
            if subtype == 'verb_definition' and value.get('type') == 'record' and value.get('record_type') == 'Verb':
                value = value['fields']['code']
                source = True
            if value.get('type') == 'redacted':
                return {'event_id': event, 'side': side, 'value': value, 'unit': 'concealed', 'range': None}
            kind = value.get('type')
            if source and kind == 'list':
                collection, minimum, unit, field = value['items'], 1, 'source-lines', 'items'
            elif kind == 'string':
                collection, minimum, unit, field = value['value'], 0, 'unicode-code-points', 'value'
            elif kind in ('list', 'tuple', 'set'):
                collection, minimum, unit, field = value['items'], 0, 'entries', 'items'
            elif kind == 'map':
                collection, minimum, unit, field = value['entries'], 0, 'entries', 'entries'
            else:
                raise ReportError('selected payload has no supported range coordinate')
            if not isinstance(start, int) or not isinstance(end, int) or start < minimum or end < start or end > len(collection) + minimum:
                raise ReportError(f'payload range is outside its half-open bounds {minimum}:{minimum + len(collection)}')
            sliced = {'type': kind, field: collection[start-minimum:end-minimum]}
            if graph:
                sliced = {'type': 'graph', 'root': sliced, 'nodes': _graph_nodes(sliced, graph['nodes'])}
            return {'event_id': event, 'side': side, 'unit': unit, 'range': [start, end],
                    'bounds': [minimum, len(collection) + minimum], 'value': sliced}
        key = _selector(object_id, rows)
        row = next((row for row in rows if _entity_key(row['entity']) == key), None)
        if row is None:
            raise ReportError('object is absent from bundle index')
        matches = [span for span in row['events'] if category is None or span['category'] == category]
        return [deepcopy(spans[span['event_id']][2]) for span in matches[offset:offset+limit]]
    except (KeyError, TypeError, AttributeError, OSError, RecursionError):
        raise ReportError('invalid or incomplete bundle') from None
