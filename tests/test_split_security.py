import hashlib

import pytest

from lambdamoo_db.split import MAGIC, SplitError, join_dir, join_parts, parse_manifest, read_manifest


def manifest(names, data=b"piece"):
    return f"{MAGIC}\n# sha256 {hashlib.sha256(data).hexdigest()}\n# bytes {len(data)}\n# pieces {len(names)}\n" + "\n".join(names) + "\n"


def test_shared_manifest_and_callback_owner():
    text = manifest(["objects/0.moo", "verbs/0.moo"], b"ab")
    digest, size, names = parse_manifest(text.encode())
    assert size == 2 and digest == hashlib.sha256(b"ab").hexdigest()
    calls = []
    assert join_parts(text, lambda name: calls.append(name) or ({"objects/0.moo": b"a", "verbs/0.moo": b"b"}[name])) == b"ab"
    assert calls == names


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/outside", "C:outside", "\\\\host\\share",
                                  "objects\\..\\outside", "./x", "objects//x", "objects/../x", "objects/./x", "objects/", ".", "..", ""])
def test_unsafe_names_rejected_before_callbacks(name):
    calls = []
    with pytest.raises(SplitError):
        join_parts(manifest(["valid.moo", name]), lambda path: calls.append(path) or b"piece")
    assert calls == []


@pytest.mark.parametrize("edit", [lambda text: text + "valid.moo\n", lambda text: text + "# bytes 5\n",
    lambda text: text.replace("# bytes 5", "# bytes -1"), lambda text: text.replace("# bytes 5", "# bytes secret"),
    lambda text: text.replace("# pieces 1", "# pieces -1"), lambda text: text.replace("# pieces 1", "# pieces 2"),
    lambda text: text.replace(hashlib.sha256(b"piece").hexdigest(), "bad"), lambda text: text.replace("# sha256", "# unknown")])
def test_invalid_manifest_metadata_rejected(edit):
    with pytest.raises(SplitError):
        parse_manifest(edit(manifest(["valid.moo"])))


def test_digest_or_size_mismatch_fails_callback_join():
    with pytest.raises(SplitError, match="mismatch"):
        join_parts(manifest(["valid.moo"]), lambda _: b"changed")


def test_manifest_cannot_use_control_characters_as_record_separators():
    text = manifest(["valid.moo"]).replace("# bytes 5\n", "# bytes 5\v")
    with pytest.raises(SplitError):
        parse_manifest(text)


def test_read_manifest_delegates_validation(tmp_path):
    (tmp_path / "MANIFEST").write_text(manifest(["../outside"]))
    with pytest.raises(SplitError):
        read_manifest(tmp_path)


@pytest.mark.parametrize("kind", ["piece", "directory", "manifest"])
def test_join_rejects_symlink_paths_before_reading(tmp_path, kind):
    root = tmp_path / "split"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "piece.moo").write_bytes(b"piece")
    (target / "MANIFEST").write_text(manifest(["piece.moo"]))
    try:
        if kind == "manifest":
            (root / "MANIFEST").symlink_to(target / "MANIFEST")
        elif kind == "directory":
            (root / "objects").symlink_to(target, target_is_directory=True)
            (root / "MANIFEST").write_text(manifest(["objects/piece.moo"]))
        else:
            (root / "piece.moo").symlink_to(target / "piece.moo")
            (root / "MANIFEST").write_text(manifest(["piece.moo"]))
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(SplitError, match="symlink|regular"):
        join_dir(root)


def test_nonregular_piece_fails(tmp_path):
    (tmp_path / "piece.moo").mkdir()
    (tmp_path / "MANIFEST").write_text(manifest(["piece.moo"]))
    with pytest.raises(SplitError, match="regular"):
        join_dir(tmp_path)


@pytest.mark.parametrize("linked_root", [False, True])
def test_trusted_root_may_have_linked_ancestor_or_be_a_link(tmp_path, linked_root):
    actual = tmp_path / "actual"
    actual.mkdir()
    root = actual if linked_root else actual / "split"
    root.mkdir(exist_ok=True)
    (root / "piece.moo").write_bytes(b"piece")
    (root / "MANIFEST").write_text(manifest(["piece.moo"]))
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    selected = alias if linked_root else alias / "split"
    assert join_dir(selected) == b"piece"


def test_join_rejects_reparse_component_below_trusted_root(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lambdamoo_db import split

    component = tmp_path / "objects"
    component.mkdir()
    (component / "piece.moo").write_bytes(b"piece")
    (tmp_path / "MANIFEST").write_text(manifest(["objects/piece.moo"]))
    original = type(component).lstat

    def reparse_status(path):
        status = original(path)
        if path == component:
            return SimpleNamespace(st_mode=status.st_mode, st_file_attributes=0x400)
        return status

    monkeypatch.setattr(split.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False)
    monkeypatch.setattr(type(component), "lstat", reparse_status)
    with pytest.raises(SplitError, match="symlink"):
        join_dir(tmp_path)
