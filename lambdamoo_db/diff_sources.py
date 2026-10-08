"""Acquire and identify immutable input bytes before comparison."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .database import MooDatabase
from .reader import load_bytes, ParseError


class SourceError(ValueError):
    """A source cannot be acquired or decoded without losing evidence."""


@dataclass
class Snapshot:
    db: MooDatabase
    metadata: dict[str, Any]


def select_objects(old: MooDatabase, new: MooDatabase, references) -> tuple[int, ...] | None:
    """Names must resolve independently to the same permanent slot on both sides."""
    from .inspection import resolve_object, LookupFailed
    if not references:
        return None
    selected = set()
    for reference in references:
        if re.fullmatch(r'#?-?\d+', reference):
            number = int(reference.lstrip('#'))
            if not any(number in db.recycled_objects or number in db.objects and not db.objects[number].anon for db in (old, new)):
                raise SourceError('Numeric object selector is absent on both sides')
        else:
            resolved = []
            for db in (old, new):
                try:
                    obj = resolve_object(db, reference)
                    resolved.append(None if obj.anon else int(obj.id))
                except (LookupFailed, KeyError, ValueError):
                    resolved.append(None)
            if resolved[0] is None or resolved[0] != resolved[1]:
                available = ', '.join('missing' if n is None else f'#{n}' for n in resolved)
                raise SourceError(f'Named selector differs between OLD and NEW ({available}); use numeric selectors')
            number = resolved[0]
        selected.add(number)
    return tuple(sorted(selected))


def _file_identity(stat: os.stat_result) -> tuple:
    # Windows stat() exposes creation time as ctime while fstat() can expose
    # last-change time. Those are not comparable across these two APIs.
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _content_digest(stream) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def snapshot_from_bytes(
    data: bytes,
    *,
    label: str = '<bytes>',
    kind: str = 'file',
    provenance: dict | None = None,
) -> Snapshot:
    """Hash and parse the same buffer; no later operation reopens its source."""
    if not isinstance(data, bytes):
        raise SourceError('Snapshot input must be immutable bytes')
    try:
        db = load_bytes(data, filename=label)
    except ParseError as error:
        raise SourceError(f'Cannot parse source: {error.code} at line {error.line}') from None
    except Exception as error:
        # Parser errors can contain the offending string. Never propagate it
        # into a diagnostic destined for an external agent.
        raise SourceError(f'Cannot parse source: {type(error).__name__}') from None
    metadata = {
        'kind': kind,
        'label': label,
        'bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        'format': db.version,
        'version_string': db.versionstring,
    }
    if provenance is not None:
        metadata['git'] = dict(provenance)
    return Snapshot(db, metadata)


def freeze_source(path: str | os.PathLike[str]) -> Snapshot:
    """Reject detectable replacement/in-place writes while freezing a file.

    The second full digest verifies the copied bytes against the still-open
    input. File identity checks bracket both reads. Parsing only uses the
    frozen buffer, so subsequent edits cannot affect a running comparison.
    """
    source = Path(path)
    try:
        stream = source.open('rb')
    except OSError:
        raise SourceError('Cannot open source file') from None
    try:
        with stream:
            before = _file_identity(os.fstat(stream.fileno()))
            if not source.is_file() or _file_identity(source.stat()) != before:
                raise SourceError('Source changed during acquisition')
            data = stream.read()
            copied_digest = hashlib.sha256(data).hexdigest()
            verified_digest = _content_digest(stream)
            after = _file_identity(os.fstat(stream.fileno()))
            named_after = _file_identity(source.stat())
            if before != after or before != named_after or copied_digest != verified_digest or len(data) != before[2]:
                raise SourceError('Source changed during acquisition')
    except OSError:
        raise SourceError('Source changed or could not be verified during acquisition') from None
    return snapshot_from_bytes(data, label=source.name)
