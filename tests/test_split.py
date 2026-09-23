from pathlib import Path

import pytest

from lambdamoo_db.reader import load
from lambdamoo_db.split import MANIFEST, SplitError, first_difference, join_dir, read_manifest, split_bytes, write_split

PROJECT_ROOT = Path(__file__).parent.parent
V17_DBS = [
    PROJECT_ROOT / "tests" / "fixtures" / "TypedMapKeys.db",
    PROJECT_ROOT / "toastcore.db",
    PROJECT_ROOT / "toast2.db",
]


@pytest.mark.parametrize("name", ["Minimal.db", "Suspended.db"])
def test_older_formats_are_rejected(name):
    data = (PROJECT_ROOT / "tests" / "fixtures" / name).read_bytes()
    with pytest.raises(SplitError, match="format version 17"):
        split_bytes(data)


@pytest.mark.parametrize("path", V17_DBS, ids=lambda p: p.name)
def test_pieces_reassemble_exactly(path):
    data = path.read_bytes()
    pieces = split_bytes(data)
    assert b"".join(piece for _, piece in pieces) == data
    names = [name for name, _ in pieces]
    assert names[0] == "header.moo"
    assert "verbs.moo" in names


def test_one_piece_per_object_record():
    path = PROJECT_ROOT / "toastcore.db"
    db = load(str(path))
    names = [name for name, _ in split_bytes(path.read_bytes())]
    object_pieces = [n for n in names if n.startswith("objects/")]
    assert len(object_pieces) == db.total_objects
    verb_owners = {int(n.split("/")[1].removesuffix(".moo")) for n in names if n.startswith("verbs/")}
    assert verb_owners == {v.object for v in db.all_verbs() if v.code is not None}


def test_write_then_join_round_trips(tmp_path):
    data = (PROJECT_ROOT / "toastcore.db").read_bytes()
    stats = write_split(data, tmp_path)
    assert stats["written"] == stats["pieces"]
    assert join_dir(tmp_path) == data
    _, size, names = read_manifest(tmp_path)
    assert size == len(data) and len(names) == stats["pieces"]


def test_resplit_rewrites_only_changed_pieces(tmp_path):
    data = (PROJECT_ROOT / "toastcore.db").read_bytes()
    write_split(data, tmp_path)
    again = write_split(data, tmp_path)
    assert again["written"] == 0 and again["removed"] == 0

    pieces = dict(split_bytes(data))
    target = next(name for name in pieces if name.startswith("verbs/"))
    old_piece = pieces[target]
    new_piece = old_piece.replace(b"\n.\n", b"\n\"edited\";\n.\n", 1)
    assert new_piece != old_piece
    changed = data.replace(old_piece, new_piece, 1)
    stats = write_split(changed, tmp_path)
    assert stats["written"] == 1
    assert (tmp_path / target).read_bytes() == new_piece
    assert join_dir(tmp_path) == changed


def test_stale_pieces_are_removed_and_other_files_kept(tmp_path):
    data = (PROJECT_ROOT / "toastcore.db").read_bytes()
    write_split(data, tmp_path)
    (tmp_path / "objects" / "999999.moo").write_bytes(b"stale")
    (tmp_path / "README").write_text("keep me")
    stats = write_split(data, tmp_path)
    assert stats["removed"] == 1
    assert not (tmp_path / "objects" / "999999.moo").exists()
    assert (tmp_path / "README").read_text() == "keep me"


def test_tampered_piece_fails_join_and_is_located(tmp_path):
    data = (PROJECT_ROOT / "toastcore.db").read_bytes()
    write_split(data, tmp_path)
    piece = tmp_path / "objects" / "1.moo"
    content = bytearray(piece.read_bytes())
    content[5] ^= 0x01
    piece.write_bytes(bytes(content))
    with pytest.raises(SplitError, match="join mismatch"):
        join_dir(tmp_path)
    assert first_difference(tmp_path, data).endswith("(offset 5 in objects/1.moo)")


def test_line_endings_are_preserved(tmp_path):
    data = (PROJECT_ROOT / "tests" / "fixtures" / "TypedMapKeys.db").read_bytes()
    assert b"\r" not in data
    write_split(data, tmp_path)
    assert b"\r" not in b"".join(p.read_bytes() for p in tmp_path.rglob("*.moo"))
    assert b"\r" not in (tmp_path / MANIFEST).read_bytes()


def test_oversized_piece_is_rejected(tmp_path):
    data = (PROJECT_ROOT / "toastcore.db").read_bytes()
    with pytest.raises(SplitError, match="exceed"):
        write_split(data, tmp_path, max_piece_bytes=1000)
    assert not (tmp_path / MANIFEST).exists()
