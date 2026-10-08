"""Assemble verified pair bundles into a pinned first-parent timeline."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile

from .compare import compare_snapshots
from .diff_git import GitRepository
from .diff_sources import SourceError, select_objects
from .diff_types import ReportOptions, canonical_json, POLICY_VERSION, KINDS


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _json_file(path, value):
    path.write_bytes(canonical_json(value) + b'\n')


def _rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def _linked(path):
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _safe_file(root, name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('Invalid history artifact path')
    parsed = PurePosixPath(name)
    if parsed.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('Invalid history artifact path')
    path = root
    for part in parsed.parts:
        path = path / part
        if _linked(path):
            raise ValueError('History artifact path cannot follow links')
    if not path.is_file():
        raise ValueError('Missing history artifact')
    return path


def validate_history(path):
    """Authenticate every advertised artifact and each successful pair."""
    from .diff_report import validate_bundle
    root = Path(path)
    if _linked(root):
        raise ValueError('History root cannot be a link')
    try:
        manifest = json.loads(_safe_file(root, 'manifest.json').read_bytes())
        if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or manifest.get('report_type') != 'history' or manifest.get('complete') is not True:
            raise ValueError('Incomplete or unsupported history manifest')
        if not isinstance(manifest.get('files'), dict) or 'edges.jsonl' not in manifest['files'] or 'objects.jsonl' not in manifest['files']:
            raise ValueError('Incomplete history file inventory')
        for name, evidence in manifest['files'].items():
            data = _safe_file(root, name).read_bytes()
            if len(data) != evidence['bytes'] or _digest(data) != evidence['sha256']:
                raise ValueError('History artifact integrity failure')
        edges = _rows(root / 'edges.jsonl')
        if len(edges) != manifest['edge_count']:
            raise ValueError('History edge inventory mismatch')
        seen = set()
        previous_commit = manifest['old_commit']
        pair_footers = []
        for edge in edges:
            identity = edge['edge_id']
            if identity in seen or not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
                raise ValueError('Invalid or duplicate history edge identity')
            seen.add(identity)
            if edge['old_commit'] != previous_commit:
                raise ValueError('History edge chain is discontinuous')
            previous_commit = edge['new_commit']
            if edge['status'] != 'error' or edge.get('bundle'):
                expected = 'edges/' + identity
                if edge.get('bundle') != expected:
                    raise ValueError('Invalid history edge bundle path')
                pair = validate_bundle(root / expected)
                header = pair['header']
                footer = pair['footer']
                calculated = _digest(canonical_json({'old_commit': edge['old_commit'], 'new_commit': edge['new_commit'],
                                                    'old_sha256': edge['sources']['old']['sha256'], 'new_sha256': edge['sources']['new']['sha256']}))
                if calculated != identity or header.get('sources') != edge['sources'] or header.get('edge') != {
                    'edge_id': identity, 'old_commit': edge['old_commit'], 'new_commit': edge['new_commit']}:
                    raise ValueError('History edge provenance differs from its pair bundle')
                for key in ('status','scan_complete','identity_complete','details_complete','payloads_complete','selected_changes'):
                    if footer.get(key) != edge.get(key):
                        raise ValueError('History edge summary differs from its pair bundle')
                semantic = manifest['semantic_options']
                expected_options = dict(semantic['options'])
                actual_options = dict(header['options'])
                expected_options.update(kinds=semantic['kinds'],max_events=semantic['max_events'])
                if semantic['references']:
                    # Named selectors were resolved independently at each edge;
                    # their numeric outcome is stored in the pair, not inferred.
                    expected_options['object_ids'] = actual_options['object_ids']
                if actual_options != expected_options or header.get('policy_version') != semantic['policy_version'] or header.get('redaction',{}).get('policy_sha256') != semantic['redaction_policy']:
                    raise ValueError('History semantic options differ from its pair bundle')
                pair_footers.append(footer)
            else:
                calculated = _digest(canonical_json({'old_commit':edge['old_commit'],'new_commit':edge['new_commit'],'failed':True}))
                if calculated != identity or edge.get('scan_complete') is not False or edge.get('identity_complete') is not False:
                    raise ValueError('Invalid failed history edge')
                pair_footers.append({'status':'error','scan_complete':False,'identity_complete':False,'details_complete':False,'payloads_complete':False})
        if previous_commit != manifest['new_commit']:
            raise ValueError('History endpoints differ from its edge chain')
        statuses = [row['status'] for row in pair_footers]
        status = 'error' if manifest.get('errors') or 'error' in statuses else 'different' if 'different' in statuses else 'unknown' if 'unknown' in statuses else 'equal'
        if manifest.get('status') != status:
            raise ValueError('History status contradicts its edges')
        for key in ('scan_complete','identity_complete','details_complete','payloads_complete'):
            expected_flag = all(row[key] for row in pair_footers)
            if key == 'scan_complete' and manifest.get('errors'):
                expected_flag = False
            if type(manifest.get(key)) is not bool or manifest[key] != expected_flag:
                raise ValueError('History completeness contradicts its edges')
        return manifest
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError):
        raise ValueError('Invalid history bundle structure') from None


def build_history(repo_path, start, end, output_dir, *, mode='endpoints', options=None,
                  references=(), kinds=(), max_events=None, max_edges=100, keep_going=False,
                  split_prefix='db', legacy_path='mongoose.db.new', source_layout='auto',
                  redaction=None, redact_strict=False, resume_from=None, selection=None):
    """Resume into a new destination only; previously published reports are read-only."""
    from .diff_report import prepare_report, write_bundle, validate_bundle, _publish
    options = options or ReportOptions()
    if options.stop_after and redaction is not None:
        raise ValueError('--stop-after cannot be combined with --redact')
    destination = Path(output_dir)
    if destination.exists() or destination.is_symlink():
        raise ValueError('History destination already exists')
    repo = GitRepository(repo_path)
    old_commit, new_commit = repo.resolve(start), repo.resolve(end)
    pairs = repo.pairs(old_commit, new_commit, mode=mode, max_edges=max_edges)
    inventory = repo.inventory(new_commit)
    semantic = {'policy_version': POLICY_VERSION, 'options': options.normalized(), 'references': list(references),
                'kinds': [kind for kind in KINDS if kind in kinds], 'max_events': max_events, 'split_prefix': split_prefix,
                'legacy_path': legacy_path, 'source_layout': source_layout,
                'redaction_policy': getattr(redaction, 'digest', None)}
    reuse = {}
    if resume_from:
        previous = validate_history(resume_from)
        if previous.get('semantic_options') == semantic:
            reuse = {e['edge_id']: e for e in _rows(Path(resume_from) / 'edges.jsonl') if e.get('bundle')}
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.moodb-history-', dir=destination.parent))
    edges, objects, statuses, omitted_parents = [], {}, [], []
    failures = []
    requested_new_commit = new_commit
    reused = 0
    aggregate_matches = [0] * len(redaction.rules) if redaction is not None else None
    cache = {}

    def snapshot(commit):
        if commit not in cache:
            # The initial release fully materializes each snapshot; this two
            # endpoint cache only avoids decoding the shared adjacent endpoint.
            if len(cache) >= 2:
                cache.pop(next(iter(cache)))
            cache[commit] = repo.snapshot(commit, split_prefix=split_prefix, legacy_path=legacy_path, source_layout=source_layout)
        return cache[commit]

    try:
        (stage / 'edges').mkdir()
        (stage / 'timelines').mkdir()
        for old_sha, new_sha in pairs:
            edge = {'old_commit': old_sha, 'new_commit': new_sha}
            pair_path = None
            pending_objects = []
            pending_parents = []
            try:
                old, new = snapshot(old_sha), snapshot(new_sha)
                identity = _digest(canonical_json({'old_commit': old_sha, 'new_commit': new_sha,
                                                  'old_sha256': old.metadata['sha256'], 'new_sha256': new.metadata['sha256']}))
                edge.update(edge_id=identity, sources={'old': old.metadata, 'new': new.metadata}, bundle='edges/' + identity)
                pair_path = stage / edge['bundle']
                selected = replace(options, object_ids=select_objects(old.db, new.db, references) if references else options.object_ids)
                if redaction is not None:
                    redaction.bind(old.db, new.db, selected)
                    matches = list(redaction.match_counts)
                    aggregate_matches = matches if aggregate_matches is None else [a + b for a, b in zip(aggregate_matches, matches)]
                if identity in reuse:
                    pair_manifest = validate_bundle(Path(resume_from) / reuse[identity]['bundle'])
                    shutil.copytree(Path(resume_from) / reuse[identity]['bundle'], stage / edge['bundle'])
                    footer = pair_manifest
                    reused += 1
                else:
                    comparison = compare_snapshots(old.db, new.db, selected)
                    comparison.header['sources'] = edge['sources']
                    comparison.header['edge'] = {'edge_id': identity, 'old_commit': old_sha, 'new_commit': new_sha}
                    report = prepare_report(comparison, kinds=kinds, max_events=max_events, redaction=redaction, redact_strict=False)
                    pair_manifest = write_bundle(report, stage / edge['bundle'])
                    footer = report.footer
                edge.update(status=footer['status'], scan_complete=footer['scan_complete'],
                            identity_complete=footer['identity_complete'], selected_changes=footer['selected_changes'],
                            details_complete=footer['details_complete'], payloads_complete=footer['payloads_complete'])
                edge['commits'] = {'old': repo.commit_metadata(old_sha), 'new': repo.commit_metadata(new_sha)}
                for metadata in edge['commits'].values():
                    if len(metadata['parents']) > 1:
                        pending_parents.append({'commit': metadata['commit'], 'parents': metadata['parents'][1:]})
                for row in _rows(stage / edge['bundle'] / 'objects.jsonl'):
                    pending_objects.append(row)
                for row in pending_objects:
                    entity = row['entity']
                    key = (entity['type'], entity['id'])
                    objects.setdefault(key, {'entity': entity, 'edges': []})['edges'].append(identity)
                omitted_parents.extend(pending_parents)
            except (SourceError, ValueError, OSError, KeyboardInterrupt) as error:
                # A pair is not part of the completed timeline until its
                # metadata and index have also been authenticated. Remove only
                # our exact staging copy, never a previous report or user dir.
                if pair_path is not None and pair_path.exists():
                    resolved = pair_path.resolve()
                    if not resolved.is_relative_to(stage.resolve()) or pair_path.is_symlink():
                        raise ValueError('Unsafe task-owned edge staging path') from None
                    shutil.rmtree(resolved)
                if not keep_going or isinstance(error, KeyboardInterrupt):
                    failures.append('History stopped at edge ' + old_sha + ' -> ' + new_sha +
                                    (': interrupted' if isinstance(error, KeyboardInterrupt) else ': ' + str(error)))
                    new_commit = edges[-1]['new_commit'] if edges else old_commit
                    break
                identity = _digest(canonical_json({'old_commit': old_sha, 'new_commit': new_sha, 'failed': True}))
                edge = {'edge_id': identity, 'old_commit': old_sha, 'new_commit': new_sha, 'status': 'error',
                        'scan_complete': False, 'identity_complete': False, 'details_complete':False,
                        'payloads_complete':False, 'errors': [str(error)]}
            edges.append(edge)
            statuses.append(edge['status'])
            if edge['status'] == 'error' and not keep_going:
                failures.append('History stopped after an error edge')
                new_commit = new_sha
                break
        strict_failure = bool(redact_strict and aggregate_matches is not None and any(count == 0 for count in aggregate_matches))
        status = 'error' if failures or strict_failure or 'error' in statuses else 'different' if 'different' in statuses else 'unknown' if 'unknown' in statuses else 'equal'
        for row in objects.values():
            entity = row['entity']
            if entity['type'] == 'permanent':
                filename = 'timelines/permanent-' + entity['id'] + '.md'
                row['timeline'] = filename
                text = f'# Object #{entity["id"]} timeline\n\n'
                text += '\n'.join(f'- [{identity}](../edges/{identity}/objects/permanent-{entity["id"]}.md)' for identity in row['edges']) + '\n'
                (stage / filename).write_text(text, encoding='utf-8', newline='\n')
        (stage / 'edges.jsonl').write_bytes(b''.join(canonical_json(row) + b'\n' for row in edges))
        ordered_objects = sorted(objects.values(), key=lambda row: (row['entity']['type'], int(row['entity']['id']) if row['entity']['type'] == 'permanent' else row['entity']['id']))
        (stage / 'objects.jsonl').write_bytes(b''.join(canonical_json(row) + b'\n' for row in ordered_objects))
        summary = f'# Database history\n\nStatus: {status}. Mode: {mode}. Edges: {len(edges)}.\n\n'
        summary += f'Pinned endpoints: `{old_commit}` -> `{new_commit}`.\n\n'
        if failures:
            summary += f'Incomplete run. Requested endpoint: `{requested_new_commit}`. Completed prefix can be reused with `--resume-from`.\n\n'
        summary += ('Only endpoints were compared; intermediate changes and reversions may be absent.\n\n' if mode == 'endpoints' else 'Timeline follows first parents; other merge parents and branch tips are inventoried in the manifest.\n\n')
        summary += '[Edge index](edges.jsonl) · [Object timeline index](objects.jsonl)\n\n'
        summary += '\n'.join(f'- {row["entity"]["type"]} {row["entity"]["id"]}: {len(row["edges"])} edges' for row in ordered_objects[:200]) + '\n'
        if len(ordered_objects) > 200:
            summary += f'\n{len(ordered_objects) - 200} additional entities are in objects.jsonl.\n'
        (stage / 'summary.md').write_text(summary, encoding='utf-8', newline='\n')
        files = {p.relative_to(stage).as_posix(): {'sha256': _digest(p.read_bytes()), 'bytes': p.stat().st_size}
                 for p in sorted(stage.rglob('*')) if p.is_file()}
        manifest = {'schema_version': 1, 'report_type': 'history', 'complete': True, 'status': status,
                    'scan_complete': not failures and not strict_failure and all(e['scan_complete'] for e in edges),
                    'identity_complete': all(e['identity_complete'] for e in edges),
                    'details_complete': all(e['details_complete'] for e in edges), 'payloads_complete': all(e['payloads_complete'] for e in edges),
                    'edge_count': len(edges), 'mode': mode, 'old_commit': old_commit, 'new_commit': new_commit,
                    'requested_new_commit': requested_new_commit,
                    'semantic_options': semantic, 'inventory': inventory, 'omitted_merge_parents': omitted_parents,
                    'selection': selection, 'reused_edges': reused, 'redaction_matches': aggregate_matches,
                    'errors': failures + (['Strict redaction rule has zero aggregate matches'] if strict_failure else []), 'files': files}
        _json_file(stage / 'manifest.json', manifest)
        validate_history(stage)
        # Use the pair publisher's exclusive destination/completion protocol
        # on POSIX as well as Windows; even a racing empty user dir is safe.
        if destination.exists() or destination.is_symlink():
            raise ValueError('History destination already exists')
        _publish(stage, destination)
        return manifest
    finally:
        if stage.exists():
            # stage is the exact task-owned temporary directory, never input.
            shutil.rmtree(stage)


def read_history(path, *, edge=None, event=None, object_id=None, category=None, offset=0, limit=50, side=None, value_range=None):
    from .diff_report import read_bundle
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError('History pagination requires nonnegative offset and positive limit')
    root = Path(path)
    manifest = validate_history(root)
    edges = _rows(root / 'edges.jsonl')
    if edge is not None:
        selected = next((row for row in edges if row['edge_id'] == edge), None)
        if selected is None or not selected.get('bundle'):
            raise ValueError('Requested history edge is missing or failed')
        return read_bundle(root / selected['bundle'], event=event, object_id=object_id, category=category,
                           offset=offset, limit=limit, side=side, value_range=value_range)
    if event is not None or side is not None or value_range is not None or category is not None:
        raise ValueError('Event or payload retrieval requires --edge in a history bundle')
    rows = _rows(root / 'objects.jsonl')
    if object_id is not None:
        number = str(int(str(object_id).lstrip('#')))
        rows = [row for row in rows if row['entity'] == {'type': 'permanent', 'id': number}]
        if not rows:
            raise ValueError('Object is absent from history index')
    return {'manifest': manifest, 'edges': edges[offset:offset + limit], 'objects': rows[offset:offset + limit]}
