"""Read pinned local Git history without consulting working files or replacements."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from .diff_sources import Snapshot, SourceError, snapshot_from_bytes
from .split import join_parts, parse_manifest, SplitError


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or '\\' in value or ':' in value or path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise SourceError('Source layout requires a safe relative path')
    return value


class GitRepository:
    """All subprocess arguments are data; blobs come from one batch reader."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.env = dict(os.environ, GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
        # --repo selects its own object/ref namespace regardless of a parent
        # shell's Git worktree or object-directory overrides.
        for name in ('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_COMMON_DIR',
                     'GIT_OBJECT_DIRECTORY','GIT_ALTERNATE_OBJECT_DIRECTORIES','GIT_NAMESPACE'):
            self.env.pop(name,None)
        if not self.path.is_dir():
            raise SourceError('Local Git repository does not exist')
        self._run('rev-parse', '--git-dir')

    def _run(self, *args: str) -> bytes:
        try:
            result = subprocess.run(['git', '-C', str(self.path), *args], capture_output=True, env=self.env, check=False)
        except OSError:
            raise SourceError('Cannot execute Git') from None
        if result.returncode:
            raise SourceError('Git revision or object is unavailable in the local repository')
        return result.stdout

    def resolve(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref or any(c in ref for c in '\x00\r\n'):
            raise SourceError('Invalid Git revision')
        commit = self._run('rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode('ascii').strip()
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
            raise SourceError('Git returned an invalid commit identity')
        return commit

    def first_parent(self, tip: str) -> list[str]:
        return self._run('rev-list', '--first-parent', self.resolve(tip), '--').decode('ascii').splitlines()

    def pairs(self, start: str, end: str, *, mode='endpoints', max_edges=100) -> list[tuple[str, str]]:
        if mode not in ('endpoints', 'adjacent') or max_edges < 1:
            raise SourceError('Invalid history mode or edge cap')
        old, new = self.resolve(start), self.resolve(end)
        if mode == 'endpoints':
            try:
                self._run('merge-base', '--is-ancestor', old, new)
            except SourceError:
                raise SourceError('Endpoints are nonancestral or their ancestry is unavailable locally') from None
            return [(old, new)]
        chain = self.first_parent(new)
        if old not in chain:
            raise SourceError('Starting revision is outside available first-parent history')
        commits = list(reversed(chain[:chain.index(old) + 1]))
        if len(commits) - 1 > max_edges:
            raise SourceError('History edge cap exceeded before loading snapshots')
        return list(zip(commits, commits[1:]))

    def resolve_date(self, pinned_tip: str, cutoff: str) -> str:
        try:
            match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})', cutoff)
            if match is None:
                raise ValueError
            whole, fraction, zone = match.groups()
            # Python 3.10 accepts only three or six fractional digits. Git
            # timestamps are whole seconds, so truncation below microseconds
            # cannot alter eligibility of a commit at the cutoff.
            fraction = '.' + (fraction[:6].ljust(6, '0')) if fraction else ''
            when = datetime.fromisoformat(whole + fraction + ('+00:00' if zone == 'Z' else zone))
        except (ValueError, TypeError):
            raise SourceError('Date cutoff requires RFC3339 with an explicit timezone') from None
        if when.tzinfo is None or 'T' not in cutoff:
            raise SourceError('Date cutoff requires an explicit timezone')
        seconds = when.timestamp()
        for commit in self.first_parent(pinned_tip):
            timestamp = int(self._run('show', '-s', '--format=%ct', '--no-show-signature', commit, '--').strip())
            if timestamp <= seconds:
                return commit
        raise SourceError('No available first-parent commit meets the date cutoff')

    def commit_metadata(self, commit: str) -> dict:
        commit = self.resolve(commit)
        raw = self._run('show', '-s', '--format=%ct%x00%cI%x00%P', '--no-show-signature', commit, '--')
        seconds, iso, parents = raw.decode('utf-8', errors='replace').strip().split('\x00', 2)
        # No recognized backup-metadata field is configured in v1. Arbitrary
        # commit prose and author timestamps cannot establish checkpoint time.
        return {'commit': commit, 'committer_time': iso, 'committer_timestamp': int(seconds),
                'parents': parents.split(), 'checkpoint_time': None}

    def inventory(self, tip: str) -> dict:
        branches = self._run('for-each-ref', '--format=%(refname)%00%(objectname)', 'refs/heads', 'refs/remotes').decode('utf-8', errors='replace').splitlines()
        return {'branch_tips': [dict(zip(('ref', 'commit'), row.split('\x00', 1))) for row in branches],
                'shallow': self._run('rev-parse', '--is-shallow-repository').strip() == b'true',
                'first_parent_tip': self.resolve(tip)}

    @contextmanager
    def _blobs(self):
        try:
            process = subprocess.Popen(['git', '-C', str(self.path), 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env)
        except OSError:
            raise SourceError('Cannot read Git blobs') from None

        def read(oid: str) -> bytes:
            process.stdin.write(oid.encode('ascii') + b'\n')
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[0].decode('ascii', errors='replace') != oid or header[1] != b'blob':
                raise SourceError('Required Git blob is missing or has an invalid type')
            try:
                size = int(header[2])
            except ValueError:
                raise SourceError('Git returned an invalid blob size') from None
            data = process.stdout.read(size)
            if len(data) != size or process.stdout.read(1) != b'\n':
                raise SourceError('Git blob was truncated')
            return data

        try:
            yield read
        finally:
            process.stdin.close()
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            process.wait()

    def snapshot(self, ref: str, *, split_prefix='db', legacy_path='mongoose.db.new', source_layout='auto') -> Snapshot:
        if source_layout not in ('auto', 'legacy'):
            raise SourceError('Invalid source layout')
        prefix, legacy = _relative_path(split_prefix), _relative_path(legacy_path)
        commit = self.resolve(ref)
        tree = {}
        for entry in self._run('ls-tree', '-r', '-z', commit, '--').split(b'\x00'):
            if not entry:
                continue
            metadata, path = entry.split(b'\t', 1)
            mode, kind, oid = metadata.decode('ascii').split()
            tree[path.decode('utf-8', errors='surrogateescape')] = (mode, kind, oid)

        def blob_id(path: str) -> str:
            if path not in tree:
                raise SourceError('Required snapshot blob is missing')
            mode, kind, oid = tree[path]
            if mode not in ('100644', '100755') or kind != 'blob':
                raise SourceError('Snapshot pieces must be regular Git blobs')
            return oid

        provenance = self.commit_metadata(commit)
        with self._blobs() as read:
            manifest_path = prefix + '/MANIFEST'
            if source_layout == 'auto' and manifest_path in tree:
                try:
                    manifest_oid = blob_id(manifest_path)
                    manifest = read(manifest_oid)
                    _, _, names = parse_manifest(manifest)
                    # Validate every path and tree mode before reading pieces.
                    ids = {name: blob_id(prefix + '/' + name) for name in names}
                    data = join_parts(manifest, lambda name: read(ids[name]))
                except SplitError:
                    raise SourceError('Invalid split snapshot manifest or piece integrity') from None
                provenance.update(layout='split', manifest_blob=manifest_oid, split_prefix=prefix)
            else:
                oid = blob_id(legacy)
                data = read(oid)
                provenance.update(layout='legacy', blob=oid, legacy_path=legacy)
        return snapshot_from_bytes(data, label=commit, kind='git', provenance=provenance)
