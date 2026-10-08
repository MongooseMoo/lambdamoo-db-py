"""A comparison must use one immutable, reproducible snapshot per side."""
from pathlib import Path

import pytest

from lambdamoo_db.diff_sources import SourceError, freeze_source, snapshot_from_bytes


FIXTURE = Path(__file__).parent / 'fixtures' / 'TypedMapKeys.db'


def test_file_and_byte_snapshot_have_same_identity_and_model():
    data = FIXTURE.read_bytes()
    frozen = freeze_source(FIXTURE)
    materialized = snapshot_from_bytes(data, label=FIXTURE.name, kind='git', provenance={'commit': 'a' * 40})
    assert frozen.metadata['sha256'] == materialized.metadata['sha256']
    assert frozen.metadata['bytes'] == len(data)
    assert frozen.db.objects[0].properties[0].value == materialized.db.objects[0].properties[0].value
    assert materialized.metadata['git']['commit'] == 'a' * 40
    assert 'path' not in frozen.metadata


def test_replacement_during_acquisition_is_rejected(tmp_path, monkeypatch):
    import lambdamoo_db.diff_sources as sources
    source = tmp_path / 'source.db'
    source.write_bytes(FIXTURE.read_bytes())
    original = sources._content_digest

    def replace_after_read(stream):
        digest = original(stream)
        replacement = tmp_path / 'replacement.db'
        replacement.write_bytes(source.read_bytes())
        replacement.replace(source)
        return digest

    monkeypatch.setattr(sources, '_content_digest', replace_after_read)
    with pytest.raises(SourceError, match='changed'):
        freeze_source(source)


def test_in_place_change_during_acquisition_is_rejected(tmp_path, monkeypatch):
    import lambdamoo_db.diff_sources as sources
    source = tmp_path / 'source.db'
    source.write_bytes(FIXTURE.read_bytes())
    original = sources._content_digest

    def change_after_read(stream):
        data = source.read_bytes()
        source.write_bytes(data.replace(b'map_test', b'map_best'))
        return original(stream)

    monkeypatch.setattr(sources, '_content_digest', change_after_read)
    with pytest.raises(SourceError, match='changed'):
        freeze_source(source)


def test_malformed_source_error_does_not_echo_value():
    secret = b'never-display-this-credential'
    with pytest.raises(SourceError) as error:
        snapshot_from_bytes(secret + b'\n', label='bad.db')
    assert secret.decode() not in str(error.value)


def test_frozen_model_is_independent_of_later_file_change(tmp_path):
    source = tmp_path / 'source.db'
    source.write_bytes(FIXTURE.read_bytes())
    frozen = freeze_source(source)
    source.write_bytes(b'not a database\n')
    assert frozen.db.version == 17
    assert frozen.metadata['bytes'] > 100
