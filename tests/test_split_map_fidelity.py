import logging
from pathlib import Path

import pytest

from lambdamoo_db.reader import ParseError, Reader, _LineSource, load, load_bytes
from lambdamoo_db.split import _BoundaryReader, join_dir, split_bytes, write_split


FIXTURE = Path(__file__).parent / 'fixtures' / 'TypedMapKeys.db'
ORIGINAL_MAP = b'10\n3\n0\n1\n2\nint\n1\n1\n2\nobj\n3\n1\n2\nerr\n'


def with_map(body, ending=b'\n'):
    original = FIXTURE.read_bytes().replace(b'\r\n', b'\n')
    assert original.count(ORIGINAL_MAP) == 1
    return original.replace(ORIGINAL_MAP, b'10\n' + body, 1).replace(b'\n', ending)


CASES = [
    ('int-float', b'2\n0\n1\n2\nfirst\n9\n1.0\n2\nsecond\n', 'map_key_loss'),
    ('int-bool', b'2\n0\n1\n2\nfirst\n14\n1\n2\nsecond\n', 'map_key_loss'),
    ('numeric-duplicates', b'2\n0\n1\n2\nfirst\n0\n4294967297\n2\nsecond\n', 'duplicate_record'),
    ('object-duplicates', b'2\n1\n1\n2\nfirst\n1\n4294967297\n2\nsecond\n', 'duplicate_record'),
    ('error-duplicates', b'2\n3\n1\n2\nfirst\n3\n4294967297\n2\nsecond\n', 'duplicate_record'),
    ('string-duplicates', b'2\n2\nKey\n2\nfirst\n2\nkey\n2\nsecond\n', 'duplicate_record'),
    ('float-zero-duplicates', b'2\n9\n0.0\n2\nfirst\n9\n-0.0\n2\nsecond\n', 'duplicate_record'),
    ('anonymous-duplicates', b'2\n12\n900\n2\nfirst\n12\n900\n2\nsecond\n', 'duplicate_record'),
    ('waif-duplicates', b'2\n13\nr 7\n.\n2\nfirst\n13\nr 7\n.\n2\nsecond\n', 'duplicate_record'),
    ('nan', b'1\n9\nnan\n2\nvalue\n', 'unsupported_map_key'),
    ('out-of-width', b'1\n0\n9223372036854775808\n2\nvalue\n', 'unsupported_map_key'),
    ('nested-value', b'1\n2\nouter\n10\n2\n0\n1\n2\nfirst\n9\n1.0\n2\nsecond\n', 'map_key_loss'),
    ('nested-key', b'1\n10\n2\n0\n1\n2\nfirst\n9\n1.0\n2\nsecond\n2\nvalue\n', 'map_key_loss'),
]


@pytest.mark.parametrize('label,body,code', CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize('ending', [b'\n', b'\r\n'], ids=['lf', 'crlf'])
def test_split_preserves_raw_map_entries_while_semantic_load_stays_strict(tmp_path, label, body, code, ending):
    data = with_map(body, ending)
    pieces = split_bytes(data)
    assert len(pieces) == len(split_bytes(FIXTURE.read_bytes()))
    assert b''.join(piece for _, piece in pieces) == data
    destination = tmp_path / 'split'
    write_split(data, destination)
    assert join_dir(destination) == data
    source = tmp_path / 'source.db'
    source.write_bytes(data)
    for loader in (lambda: load_bytes(data), lambda: load(str(source))):
        with pytest.raises(ParseError) as error:
            loader()
        assert error.value.code == code


def test_layout_scan_consumes_waif_homes_in_map_values_and_never_returns_model():
    # Both homes occur under map values; a reference follows their creation.
    body = (b'2\n0\n1\n13\nc 7\n1\n1\n1\n0\n13\nc 8\n1\n1\n0\n-1\n.\n-1\n.\n'
            b'9\n1.0\n13\nr 8\n.\n')
    data = with_map(body)
    source = _LineSource(data)
    reader = _BoundaryReader(source)
    assert reader.parse() is None
    assert reader._waif_ids == {7, 8}
    assert source.offset == len(data)
    assert b''.join(piece for _, piece in split_bytes(data)) == data


def test_layout_scan_debug_logging_does_not_require_semantic_map():
    logger = logging.getLogger('lambdamoo_db.reader')
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        data = with_map(CASES[0][1])
        assert b''.join(piece for _, piece in split_bytes(data)) == data
    finally:
        logger.setLevel(previous)


@pytest.mark.parametrize('body,code', [(b'-1\n', 'invalid_source'),
    (b'1\n0\n1\n', 'invalid_source'), (b'1\n999\n', 'invalid_source')])
def test_layout_scan_still_rejects_bad_structure(body, code):
    with pytest.raises(ParseError) as error:
        split_bytes(with_map(body))
    assert error.value.code == code


def test_default_reader_parse_still_returns_semantic_database():
    result = Reader(_LineSource(FIXTURE.read_bytes())).parse()
    assert len(result.objects[0].properties[-1].value) == 3
