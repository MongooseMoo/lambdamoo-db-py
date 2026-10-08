"""Split a v17 MOO database dump into small pieces, and join them back.

Splitting never re-encodes anything. The reader is used only to find the byte
offset where each record starts; the dump is then cut at those offsets. Joining
concatenates the pieces in MANIFEST order and checks the result against the
source's size and SHA-256, so a join is byte-identical or it fails.

Layout of a split directory::

    MANIFEST            ordered piece list, plus source sha256 and size
    header.moo          version line through the object count
    objects/<id>.moo    one object record (recycled objects included)
    anon.moo            the anonymous-object section
    verbs.moo           the verb-program count line
    verbs/<id>.moo      every verb program of object <id>
    trailer.moo         anything after the last verb program (normally absent)

MANIFEST is plain text, so a restore needs no Python::

    grep -v '^#' MANIFEST | xargs cat > restored.db
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable

from .database import MooDatabase
from .enums import DBVersions
from .reader import Reader, _LineSource

MANIFEST = "MANIFEST"
MAGIC = "# moodb-split 1"
PIECE_SUFFIX = ".moo"
# GitHub rejects files over 100 MiB; stay clear of it.
DEFAULT_MAX_PIECE_BYTES = 95 * 1024 * 1024

_V17_HEADER = f"** LambdaMOO Database, Format Version {int(DBVersions.DBV_Bool)} **".encode()
_OBJECT_LINE =re.compile(rb"#\s*(\d+)")
_VERB_LINE = re.compile(rb"#(\d+):(\d+)\r?\n")


class SplitError(Exception):
    pass


class _BoundaryReader(Reader):
    """Reader that records (byte offset, piece name) wherever a piece starts."""

    def __init__(self, source: _LineSource) -> None:
        super().__init__(source, "<split>", _layout_only=True)  # type: ignore[arg-type]
        self.source = source
        self.marks: list[tuple[int, str]] = [(0, "header" + PIECE_SUFFIX)]
        self._in_anon = False
        self._last_verb_owner: str | None = None
        self._verb_names: dict[str, int] = {}

    def parse(self) -> None:
        # The model contains nonsemantic placeholders. Only byte boundaries
        # escape this scanner; never expose it to semantic loading/comparison.
        super().parse()

    def readObject_ng(self, db: MooDatabase) -> Any:
        if self._in_anon:
            return super().readObject_ng(db)
        start = self.source.offset
        match = _OBJECT_LINE.match(self.source.data, start)
        if not match:
            self.parse_error("object record does not start with #<number>")
        self.marks.append((start, f"objects/{int(match.group(1))}{PIECE_SUFFIX}"))
        return super().readObject_ng(db)

    def readAnonObjects(self, db: MooDatabase) -> None:
        self.marks.append((self.source.offset, "anon" + PIECE_SUFFIX))
        self._in_anon = True
        try:
            super().readAnonObjects(db)
        finally:
            self._in_anon = False

    def readVerbs(self, db: MooDatabase) -> None:
        # parse_v17 has just read the verb count; its line starts this piece.
        start = self.source.last_line_start
        count_line = self.source.data[start : self.source.offset]
        if count_line not in (f"{db.total_verbs}\n".encode(), f"{db.total_verbs}\r\n".encode()):
            self.parse_error("expected verb count line")
        self.marks.append((start, "verbs" + PIECE_SUFFIX))
        super().readVerbs(db)

    def readVerb(self, db: MooDatabase) -> None:
        start = self.source.offset
        match = _VERB_LINE.match(self.source.data, start)
        if not match:
            self.parse_error("verb program does not start with #<object>:<index>")
        owner = match.group(1).decode()
        if owner != self._last_verb_owner:
            # Programs are normally grouped by object. If an object's programs
            # ever reappear later, give that run its own piece name.
            seen = self._verb_names.get(owner, 0)
            self._verb_names[owner] = seen + 1
            suffix = f"~{seen}" if seen else ""
            self.marks.append((start, f"verbs/{owner}{suffix}{PIECE_SUFFIX}"))
            self._last_verb_owner = owner
        super().readVerb(db)


def split_bytes(data: bytes) -> list[tuple[str, bytes]]:
    """Cut a v17 dump into named pieces whose concatenation is exactly ``data``."""
    first_line = data[: data.find(b"\n")]
    if first_line.rstrip(b"\r") != _V17_HEADER:
        raise SplitError("only format version 17 is supported")
    source = _LineSource(data)
    reader = _BoundaryReader(source)
    reader.parse()
    marks = reader.marks
    if source.offset < len(data):
        marks.append((source.offset, "trailer" + PIECE_SUFFIX))

    names = [name for _, name in marks]
    if len(set(names)) != len(names):
        raise SplitError("duplicate piece names")
    offsets = [offset for offset, _ in marks]
    if offsets[0] != 0 or any(a >= b for a, b in zip(offsets, offsets[1:])):
        raise SplitError("piece offsets are not strictly increasing")

    ends = offsets[1:] + [len(data)]
    pieces = [(name, data[start:end]) for (start, name), end in zip(marks, ends)]
    if b"".join(piece for _, piece in pieces) != data:
        raise SplitError("pieces do not reassemble to the source")
    return pieces


def manifest_text(pieces: list[tuple[str, bytes]], data: bytes) -> str:
    lines = [
        MAGIC,
        f"# sha256 {hashlib.sha256(data).hexdigest()}",
        f"# bytes {len(data)}",
        f"# pieces {len(pieces)}",
    ]
    lines.extend(name for name, _ in pieces)
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def write_split(
    data: bytes,
    out_dir: str | os.PathLike[str],
    max_piece_bytes: int = DEFAULT_MAX_PIECE_BYTES,
) -> dict[str, int]:
    """Split ``data`` into ``out_dir``, rewriting only pieces whose bytes changed.

    Stale ``*.moo`` pieces from an earlier split are removed. Nothing else in
    ``out_dir`` is touched. The result is joined back from disk and verified.
    """
    pieces = split_bytes(data)
    oversized = [(name, len(piece)) for name, piece in pieces if len(piece) > max_piece_bytes]
    if oversized:
        detail = ", ".join(f"{name} ({size} bytes)" for name, size in oversized)
        raise SplitError(f"pieces exceed {max_piece_bytes} bytes: {detail}")

    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    stats = {"pieces": len(pieces), "written": 0, "unchanged": 0, "removed": 0}
    wanted = {name for name, _ in pieces}
    for name, piece in pieces:
        path = root / name
        if path.is_file() and path.stat().st_size == len(piece) and path.read_bytes() == piece:
            stats["unchanged"] += 1
            continue
        _write_atomic(path, piece)
        stats["written"] += 1

    for path in root.rglob("*" + PIECE_SUFFIX):
        if path.is_file() and path.relative_to(root).as_posix() not in wanted:
            path.unlink()
            stats["removed"] += 1
    for sub in ("objects", "verbs"):
        subdir = root / sub
        if subdir.is_dir() and not any(subdir.iterdir()):
            subdir.rmdir()

    _write_atomic(root / MANIFEST, manifest_text(pieces, data).encode())
    join_dir(root)  # raises if what is on disk does not reproduce the source
    return stats


def parse_manifest(data: bytes | str) -> tuple[str, int, list[str]]:
    """Validate a manifest fully before any source pieces are accessed."""
    try:
        text = data.decode("ascii") if isinstance(data, bytes) else data
        text.encode("ascii")
    except (UnicodeError, AttributeError):
        raise SplitError("MANIFEST must be ASCII") from None
    if any(ord(char) < 32 and char not in "\r\n" or ord(char) == 127 for char in text):
        raise SplitError("MANIFEST has invalid control characters")
    if "\r" in text.replace("\r\n", ""):
        raise SplitError("MANIFEST has invalid record delimiter")
    lines = text.splitlines()
    if not lines or lines[0] != MAGIC:
        raise SplitError(f"{MANIFEST} does not start with {MAGIC!r}")
    meta = {}
    names = []
    for line in lines[1:]:
        if line.startswith("# "):
            key, _, value = line[2:].partition(" ")
            if key not in ("sha256", "bytes", "pieces") or key in meta:
                raise SplitError("MANIFEST has unknown or duplicate metadata")
            meta[key] = value
        else:
            _validate_piece_name(line)
            names.append(line)
    for key in ("sha256", "bytes", "pieces"):
        if key not in meta:
            raise SplitError(f"{MANIFEST} is missing '# {key}'")
    if not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"]):
        raise SplitError("MANIFEST has invalid sha256")
    if any(not re.fullmatch(r"[0-9]+", meta[key]) for key in ("bytes", "pieces")):
        raise SplitError("MANIFEST has invalid counts")
    try:
        pieces_count, size = int(meta["pieces"]), int(meta["bytes"])
    except ValueError:
        raise SplitError("MANIFEST has invalid counts") from None
    if pieces_count != len(names):
        raise SplitError("MANIFEST piece count mismatch")
    if len(set(names)) != len(names):
        raise SplitError("MANIFEST has duplicate piece names")
    return meta["sha256"], size, names


def _validate_piece_name(name: str) -> None:
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(part in ("", ".", "..") for part in name.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise SplitError("MANIFEST has unsafe piece name")


def _regular_path(root: Path, name: str) -> Path:
    """Resolve the caller's trusted root, then refuse links beneath it."""
    _validate_piece_name(name)
    try:
        trusted_root = root.resolve(strict=True)
        if not trusted_root.is_dir():
            raise SplitError("split parent is not a directory")
    except (OSError, RuntimeError):
        raise SplitError("split path is missing or unreadable") from None
    path = trusted_root
    parts = name.split("/")
    for index, part in enumerate(parts):
        path = path / part
        try:
            status = path.lstat()
            mode = status.st_mode
        except OSError:
            raise SplitError("split path is missing or unreadable") from None
        if stat.S_ISLNK(mode) or getattr(status, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise SplitError("split path contains a symlink")
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise SplitError("split parent is not a directory")
    if not stat.S_ISREG(mode):
        raise SplitError("split piece is not a regular file")
    return path


def read_manifest(in_dir: str | os.PathLike[str]) -> tuple[str, int, list[str]]:
    """Return validated (sha256, size, names) from a regular MANIFEST."""
    return parse_manifest(_regular_path(Path(in_dir), MANIFEST).read_bytes())


def join_parts(manifest: bytes | str, read_piece: Callable[[str], bytes]) -> bytes:
    """Join validated ordered pieces from a filesystem or immutable Git source."""
    sha256, size, names = parse_manifest(manifest)
    pieces = []
    for name in names:
        piece = read_piece(name)
        if not isinstance(piece, bytes):
            raise SplitError("piece callback did not return bytes")
        pieces.append(piece)
    data = b"".join(pieces)
    actual = hashlib.sha256(data).hexdigest()
    if len(data) != size or actual != sha256:
        raise SplitError(f"join mismatch: expected {size} bytes sha256 {sha256}, got {len(data)} bytes sha256 {actual}")
    return data


def join_dir(in_dir: str | os.PathLike[str]) -> bytes:
    """Reassemble a split directory and verify it against its MANIFEST."""
    root = Path(in_dir)
    manifest = _regular_path(root, MANIFEST).read_bytes()
    _, _, names = parse_manifest(manifest)
    # Preflight all paths before opening any piece.
    paths = {name: _regular_path(root, name) for name in names}
    return join_parts(manifest, lambda name: paths[name].read_bytes())


def first_difference(in_dir: str | os.PathLike[str], original: bytes) -> str | None:
    """Describe where a split directory first diverges from ``original``, or None."""
    root = Path(in_dir)
    _, _, names = read_manifest(root)
    paths = {name: _regular_path(root, name) for name in names}
    offset = 0
    for name in names:
        piece = paths[name].read_bytes()
        expected = original[offset : offset + len(piece)]
        if piece != expected:
            index = next((i for i, (a, b) in enumerate(zip(piece, expected)) if a != b), min(len(piece), len(expected)))
            return f"byte {offset + index} (offset {index} in {name})"
        offset += len(piece)
    if offset != len(original):
        return f"joined output has {offset} bytes, original has {len(original)}"
    return None
