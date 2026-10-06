"""Open a v17 dump without parsing all of it.

One full parse records the byte offset of every object record and verb
program. That index is small and is kept in the cache directory. With it, a
LazyDatabase parses only the records a query touches, straight from the dump,
so looking at one verb costs a few file reads instead of a whole parse.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import attrs

from .database import MooDatabase, MooObject, Waif, WaifReference
from .enums import DBVersions
from .reader import Reader, detect_line_ending, load
from .split import _LineSource

_V17_HEADER = f"** LambdaMOO Database, Format Version {int(DBVersions.DBV_Bool)} **".encode()
_OBJECT_LINE = re.compile(rb"#\s*(\d+)")
_VERB_LINE = re.compile(rb"#(\d+):(\d+)\r?\n")

# Modules whose code decides what the index holds and what a record parses to.
_INDEX_KEY_MODULES = ("reader.py", "database.py", "enums.py", "templates.py", "split.py", "lazy.py")


@attrs.frozen
class DumpIndex:
    """Where the records of one dump are, as byte offsets into it."""

    header_end: int  # the first object record starts here
    records: dict[int, tuple[int, int, bool]]  # object -> (start, end, anonymous), in dump order
    names: dict[int, str]  # object -> name, so naming or finding an object reads no record
    programs: dict[int, list[tuple[int, int]]]  # object -> (start, end) of each of its verb programs
    waif_homes: dict[int, int | None]  # waif -> the object whose record defines it; None for the header
    recycled: frozenset[int]
    total_verbs: int


class _IndexReader(Reader):
    """Reader that notes where each object record, verb program and waif definition is."""

    def __init__(self, source: _LineSource) -> None:
        super().__init__(source, "<index>")  # type: ignore[arg-type]
        self.source = source
        self.header_end = 0
        self.records: dict[int, tuple[int, int, bool]] = {}
        self.programs: dict[int, list[tuple[int, int]]] = {}
        self.waif_homes: dict[int, int | None] = {}
        self._home: int | None = None  # the object whose record is being read
        self._in_anon = False

    def readObjects(self, db: MooDatabase) -> None:
        self.header_end = self.source.offset
        super().readObjects(db)

    def readAnonObjects(self, db: MooDatabase) -> None:
        self._in_anon = True
        super().readAnonObjects(db)
        self._in_anon = False

    def readObject_ng(self, db: MooDatabase) -> Any:
        start = self.source.offset
        match = _OBJECT_LINE.match(self.source.data, start)
        if not match:
            self.parse_error("object record does not start with #<number>")
        self._home = int(match.group(1))
        obj = super().readObject_ng(db)
        self._home = None
        if obj is not None:
            if obj.id in self.records:
                self.parse_error(f"object #{obj.id} has two records")
            self.records[obj.id] = (start, self.source.offset, self._in_anon)
        return obj

    def readVerb(self, db: MooDatabase) -> None:
        start = self.source.offset
        match = _VERB_LINE.match(self.source.data, start)
        if not match:
            self.parse_error("verb program does not start with #<object>:<index>")
        super().readVerb(db)
        self.programs.setdefault(int(match.group(1)), []).append((start, self.source.offset))

    def readWaif(self, db: MooDatabase) -> WaifReference:
        ref = super().readWaif(db)
        # A waif is written out in full the first time the dump reaches it.
        self.waif_homes.setdefault(ref.index, self._home)
        return ref


def build_index(db_path: str | os.PathLike[str]) -> tuple[DumpIndex, MooDatabase]:
    """Parse a whole v17 dump once, returning its index and the parsed database."""
    data = Path(db_path).read_bytes()
    reader = _IndexReader(_LineSource(data))
    db = reader.parse()
    db.line_ending = detect_line_ending(data[:1000])
    index = DumpIndex(
        header_end=reader.header_end,
        records=reader.records,
        names=db.object_names(),
        programs=reader.programs,
        waif_homes={i: reader.waif_homes[i] for i in db.waifs},
        recycled=frozenset(db.recycled_objects),
        total_verbs=db.total_verbs,
    )
    return index, db


class _LazyObjects(Mapping[int, MooObject]):
    """``db.objects`` for a LazyDatabase: every object is listed, and parsed when first read."""

    def __init__(self, db: LazyDatabase) -> None:
        self._db = db
        self.loaded: dict[int, MooObject] = {}

    def __getitem__(self, oid: int) -> MooObject:
        if oid in self.loaded:
            return self.loaded[oid]
        if oid not in self._db.index.records:
            raise KeyError(oid)
        return self._db.load_object(oid)

    def __contains__(self, oid: object) -> bool:
        return oid in self._db.index.records

    def __iter__(self) -> Iterator[int]:
        return iter(self._db.index.records)

    def __len__(self) -> int:
        return len(self._db.index.records)

    def values(self):  # type: ignore[override]
        self._db.load_all()
        return super().values()

    def items(self):  # type: ignore[override]
        self._db.load_all()
        return super().items()


class _LazyWaifs(Mapping[int, Waif]):
    """``db.waifs`` for a LazyDatabase: a waif is read by parsing the record that defines it."""

    def __init__(self, db: LazyDatabase) -> None:
        self._db = db
        self.loaded: dict[int, Waif] = {}

    def __setitem__(self, index: int, waif: Waif) -> None:
        self.loaded[index] = waif

    def __getitem__(self, index: int) -> Waif:
        if index not in self.loaded:
            self._db.objects[self._db.index.waif_homes[index]]  # type: ignore[index]  # parsing it defines the waif
        return self.loaded[index]

    def __contains__(self, index: object) -> bool:
        return index in self._db.index.waif_homes

    def __iter__(self) -> Iterator[int]:
        return iter(self._db.index.waif_homes)

    def __len__(self) -> int:
        return len(self._db.index.waif_homes)


class LazyDatabase(MooDatabase):
    """A v17 dump whose objects and waifs are parsed from the file when first asked for.

    The header (players, tasks, connections) is small and is parsed on opening.
    """

    def __init__(self, db_path: str | os.PathLike[str], index: DumpIndex) -> None:
        super().__init__()
        self.path = Path(db_path)
        self.index = index
        self._all_loaded = False
        self.objects = _LazyObjects(self)  # type: ignore[assignment]
        self.waifs = _LazyWaifs(self)  # type: ignore[assignment]
        header = self._read(0, index.header_end)
        self.line_ending = detect_line_ending(header[:1000])
        reader = Reader(_LineSource(header), str(self.path))  # type: ignore[arg-type]
        reader.read_version(self)
        reader.read_v17_header(self)
        self.total_verbs = index.total_verbs
        self.recycled_objects = set(index.recycled)

    def _read(self, start: int, end: int) -> bytes:
        # Opened per read: a handle held open would stop the dump being replaced on Windows.
        with open(self.path, "rb") as f:
            f.seek(start)
            return f.read(end - start)

    def _reader(self, start: int, end: int) -> Reader:
        return Reader(_LineSource(self._read(start, end)), str(self.path))  # type: ignore[arg-type]

    def object_names(self) -> dict[int, str]:
        return self.index.names

    def name_of(self, oid: int) -> str | None:
        return self.index.names.get(oid)

    def load_object(self, oid: int) -> MooObject:
        start, end, anon = self.index.records[oid]
        reader = self._reader(start, end)
        obj = reader.readObject_ng(self)
        obj.anon = anon
        # Listed before its ancestors and programs are read, since both look it up.
        self.objects.loaded[oid] = obj  # type: ignore[attr-defined]
        reader.process_propnames(self, obj)
        self._read_programs(obj)
        return obj

    def _read_programs(self, obj: MooObject) -> None:
        for start, end in self.index.programs.get(obj.id, ()):
            reader = self._reader(start, end)
            _, verb_number, code = reader.read_program()
            reader.attach_program(obj, verb_number, code)

    def load_all(self) -> None:
        """Parse everything not yet loaded, in one pass.

        Asking for all the objects means a scan of the whole dump, and one
        sequential parse is quicker than reading every record separately.
        """
        if self._all_loaded:
            return
        full = load(str(self.path))
        for oid, obj in full.objects.items():
            self.objects.loaded.setdefault(oid, obj)  # type: ignore[attr-defined]
        for index, waif in full.waifs.items():
            self.waifs.loaded.setdefault(index, waif)  # type: ignore[attr-defined]
        self._all_loaded = True

    def objects_with_programs(self) -> list[MooObject]:
        """Every object that has verb code, in object order, for searching the code.

        An object not loaded yet is read without its properties, which are most
        of a dump, so these are not kept as ``objects`` entries.
        """
        found = []
        for oid in sorted(self.index.programs):
            obj = self.objects.loaded.get(oid)  # type: ignore[attr-defined]
            if obj is None:
                start, end, anon = self.index.records[oid]
                obj = self._reader(start, end).read_object_head_ng(self)
                obj.anon = anon
                self._read_programs(obj)
            found.append(obj)
        return found


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".cache") / "lambdamoo-db"


def _cache_prefix(db_path: Path) -> str:
    return hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()[:16]


def index_path_for(db_path: Path, cache_dir: Path) -> Path:
    """``<path hash>-<content key hash>.index``; the key covers the dump and the parser."""
    st = db_path.stat()
    here = Path(__file__).parent
    parts = [str(st.st_size), str(st.st_mtime_ns), f"{sys.version_info[0]}.{sys.version_info[1]}"]
    parts += [hashlib.sha256((here / m).read_bytes()).hexdigest() for m in _INDEX_KEY_MODULES]
    key = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return cache_dir / f"{_cache_prefix(db_path)}-{key}.index"


def open_indexed(db_path: str | os.PathLike[str], cache_dir: Path) -> MooDatabase:
    """Open a dump through its offset index, building the index on first use.

    The index is keyed on the dump's path, size and mtime and on the parser
    source. Writing one removes the stale indexes (and the pickles of earlier
    versions of this tool) for the same dump path. Formats older than 17 have
    no index and are parsed whole.
    """
    db_path = Path(db_path)
    with open(db_path, "rb") as f:
        first_line = f.readline()
    if first_line.rstrip(b"\r\n") != _V17_HEADER:
        return load(str(db_path))
    index_path = index_path_for(db_path, cache_dir)
    if index_path.exists():
        return LazyDatabase(db_path, pickle.loads(index_path.read_bytes()))
    index, db = build_index(db_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = index_path.with_name(f"{index_path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(pickle.dumps(index, protocol=pickle.HIGHEST_PROTOCOL))
    os.replace(tmp, index_path)
    for suffix in ("index", "pickle"):
        for stale in cache_dir.glob(f"{_cache_prefix(db_path)}-*.{suffix}"):
            if stale != index_path:
                stale.unlink(missing_ok=True)
    return db
