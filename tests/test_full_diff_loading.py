from io import StringIO
import logging
from pathlib import Path

import pytest

from lambdamoo_db.database import MooDatabase
from lambdamoo_db.reader import ParseError, Reader, load, load_bytes
from lambdamoo_db.map_keys import NUMERIC_MAP_KEY_POLICY, numeric_key_identity
from lambdamoo_db.split import _LineSource
from lambdamoo_db.writer import Writer


def object_record(oid=0, name="synthetic", *, verbs=0):
    return (f"#{oid}\n{name}\n0\n0\n1\n-1\n10\n0\n4\n0\n1\n-1\n4\n0\n{verbs}\n"
            + "v\n0\n0\n-1\n" * verbs + "0\n0\n").encode("latin-1")


def database_bytes(records=(), *, anonymous=b"0\n", programs=b"0\n"):
    header = ("** LambdaMOO Database, Format Version 17 **\n0\n"
              "0 values pending finalization\n0 clocks\n0 queued tasks\n"
              "0 suspended tasks\n0 interrupted tasks\n0 active connections\n"
              f"{len(records)}\n").encode()
    return header + b"".join(records) + anonymous + programs


@pytest.mark.parametrize("data, expected", [(b"a\rb\n", "a\rb"), (b"a\r\r\n", "a\r"),
                                           (b"a\r\n", "a"), (b"a\r", "a\r")])
def test_record_delimiter_removes_only_one(data, expected):
    reader = Reader(_LineSource(data))
    assert reader.readString() == expected
    with pytest.raises(Exception, match="[Ee][Oo][Ff]|end of"):
        reader.readString()


def test_immutable_bytes_and_file_loading_share_decoder(tmp_path):
    data = database_bytes([object_record(name="name\rwith bare CR")])
    path = tmp_path / "synthetic.db"
    path.write_bytes(data)
    for db in [load_bytes(data), load(str(path))]:
        assert db.objects[0].name == "name\rwith bare CR"
        output = StringIO()
        Writer(db=db, output_file=output).writeDatabase()
        assert output.getvalue().encode("latin-1") == data


def test_unterminated_source_fails_promptly():
    with pytest.raises(Exception, match="[Ee][Oo][Ff]|end of"):
        Reader(_LineSource(b"secret program\n")).readCode()


def test_crlf_dump_preserves_extra_trailing_cr_data():
    data = database_bytes([object_record(name="name\r")]).replace(b"\n", b"\r\n")
    db = load_bytes(data)
    assert db.objects[0].name == "name\r"
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    assert output.getvalue().encode("latin-1") == data


@pytest.mark.parametrize("bad", [b"secret-number\n", b"secret-float\n"])
def test_numeric_errors_do_not_expose_values(bad):
    reader = Reader(_LineSource(bad))
    with pytest.raises(Exception) as error:
        (reader.readInt if b"number" in bad else reader.readFloat)()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("data, method, code", [(b"", "readString", "unexpected_eof"),
    (b"secret\n", "readInt", "invalid_source")])
def test_safe_parse_error_metadata(data, method, code):
    reader = Reader(_LineSource(data))
    with pytest.raises(ParseError) as error:
        getattr(reader, method)()
    assert error.value.line == 1
    assert error.value.code == code


def test_debug_parse_diagnostics_do_not_include_raw_values(caplog):
    with caplog.at_level(logging.DEBUG, logger="lambdamoo_db.reader"):
        Reader(_LineSource(b"2\nsecret-value\n")).readValue(MooDatabase())
        load_bytes(database_bytes([object_record(name="secret-object-name")]))
    assert "secret" not in caplog.text


@pytest.mark.parametrize("keys", [b"0\n1\n14\n1\n", b"0\n1\n9\n1.0\n",
                                  b"9\n0.0\n9\n-0.0\n", b"2\nSecret\n2\nSECRET\n"])
def test_collapsing_or_server_equivalent_map_keys_are_rejected(keys):
    # Split into two encoded keys (every scalar here has a tag and value).
    lines = keys.splitlines(keepends=True)
    data = b"2\n" + b"".join(lines[:2]) + b"0\n7\n" + b"".join(lines[2:]) + b"0\n8\n"
    with pytest.raises(Exception, match="map") as error:
        Reader(_LineSource(data)).readMap(MooDatabase())
    assert "Secret" not in str(error.value) and "SECRET" not in str(error.value)


@pytest.mark.parametrize("key", [b"9\nnan\n", b"4\n0\n", b"10\n0\n", b"5\n", b"6\n", b"7\n0\n", b"8\n0\n"])
def test_unsupported_map_keys_fail_without_rendering(key):
    with pytest.raises(Exception, match="map"):
        Reader(_LineSource(b"1\n" + key + b"0\n7\n")).readMap(MooDatabase())


def test_nul_string_map_key_fails_explicitly():
    with pytest.raises(Exception, match="map"):
        Reader(_LineSource(b"1\n2\nsecret\x00key\n0\n7\n")).readMap(MooDatabase())


@pytest.mark.parametrize("body", [b"r 0\nsecret\n", b"c 0\n0\n0\n0\n-1\nsecret\n"])
def test_malformed_waif_terminator_fails_without_value(body):
    with pytest.raises(Exception) as error:
        Reader(_LineSource(body)).readWaif(MooDatabase())
    assert "secret" not in str(error.value)


def test_legal_typed_map_keys_and_insertion_order_survive():
    data = b"4\n0\n1\n2\nint\n1\n1\n2\nobj\n3\n1\n2\nerr\n14\n0\n2\nbool\n"
    result = Reader(_LineSource(data)).readMap(MooDatabase())
    assert list(result.values()) == ["int", "obj", "err", "bool"]


@pytest.mark.parametrize('tag,number', [(tag, number) for tag in (0, 1, 3)
    for number in (-(2**63)-1, 2**63, 2**80)])
def test_wide_numeric_map_keys_fail_explicitly_without_echoing_data(tag, number):
    encoded = f'1\n{tag}\n{number}\n2\nsecret-value\n'.encode()
    with pytest.raises(ParseError) as error:
        Reader(_LineSource(encoded)).readMap(MooDatabase())
    assert error.value.code == 'unsupported_map_key'
    assert str(number) not in str(error.value) and 'secret' not in str(error.value)


@pytest.mark.parametrize('tag,number', [(tag, number) for tag in (0, 1, 3)
    for number in (-(2**63), 2**63-1, -(2**31)-1, 2**31, 2**32+1, (2**47)+18)])
def test_configured_numeric_map_key_wire_boundaries_survive(tag, number):
    encoded = f'1\n{tag}\n{number}\n0\n7\n'.encode()
    result = Reader(_LineSource(encoded)).readMap(MooDatabase())
    assert [int(key) for key in result] == [number]
    assert list(result.values()) == [7]


@pytest.mark.parametrize('tag', [0, 1, 3])
def test_server_narrowing_equivalent_serialized_keys_fail_explicitly(tag):
    encoded = f'2\n{tag}\n1\n0\n7\n{tag}\n4294967297\n0\n8\n'.encode()
    with pytest.raises(ParseError) as error:
        Reader(_LineSource(encoded)).readMap(MooDatabase())
    assert error.value.code == 'duplicate_record'


def test_configured_numeric_key_policy_identity_and_widths():
    from lambdamoo_db.database import ObjNum, MooError
    assert NUMERIC_MAP_KEY_POLICY == 'toast-num64-int32-gnu-v1-error-union64'
    for cls in (int, ObjNum, MooError):
        assert numeric_key_identity(cls(1)) == numeric_key_identity(cls(4294967297)) == 1
        assert numeric_key_identity(cls(-(2**63))) == 0
        assert numeric_key_identity(cls(2**63-1)) == 0xffffffff
    assert numeric_key_identity(MooError(-1)) == 0xffffffff
    assert numeric_key_identity(MooError((2**47)+18)) == 18
    for bad in (True, 1.0, 'secret', 2**63, ObjNum(-(2**63)-1), MooError(2**63)):
        with pytest.raises(ValueError) as error:
            numeric_key_identity(bad)
        assert str(error.value) == 'unsupported numeric map key'


@pytest.mark.parametrize('tag', [0, 1, 3])
def test_wide_numeric_values_retain_precision(tag):
    number = 2**80
    value = Reader(_LineSource(f'{tag}\n{number}\n'.encode())).readValue(MooDatabase())
    assert int(value) == number


def test_anonymous_and_waif_dump_ids_are_not_numeric_server_map_keys():
    number = 2**80
    for key in (f'12\n{number}\n', f'13\nr {number}\n.\n'):
        result = Reader(_LineSource(('1\n' + key + '0\n7\n').encode())).readMap(MooDatabase())
        assert list(result.values()) == [7]


@pytest.mark.parametrize("records, anon", [([object_record(), object_record()], b"0\n"),
    ([object_record(), b"#0 recycled\n"], b"0\n"), ([b"#1 recycled\n"] * 2, b"0\n"),
    ([object_record()], b"1\n" + object_record() + b"0\n")])
def test_duplicate_object_record_ids_fail(records, anon):
    with pytest.raises(Exception, match="duplicate"):
        load_bytes(database_bytes(records, anonymous=anon))


def test_recycled_and_anon_counts_use_serialized_sections():
    db = load_bytes(database_bytes([object_record(), b"#1 recycled\n"],
                                   anonymous=b"1\n" + object_record(2) + b"0\n"))
    assert db.total_objects == 2
    assert set(db.objects) == {0, 2} and db.objects[2].anon
    assert db.recycled_objects == {1}


def test_repeated_program_definition_fails():
    with pytest.raises(Exception, match="duplicate"):
        load_bytes(database_bytes([object_record(verbs=1)], programs=b"2\n#0:0\n.\n#0:0\n.\n"))


def test_repeated_waif_definition_fails():
    reader = Reader(_LineSource(b"c 0\n0\n0\n0\n-1\n.\n" * 2))
    db = MooDatabase()
    reader.readWaif(db)
    with pytest.raises(Exception, match="duplicate"):
        reader.readWaif(db)


@pytest.mark.parametrize("count", ["-1", "secret"])
def test_invalid_counts_fail(count):
    with pytest.raises(Exception):
        Reader(_LineSource((count + "\n").encode())).readList(MooDatabase())


@pytest.mark.parametrize("name", ["Minimal.db", "Suspended.db", "TypedMapKeys.db"])
def test_supported_fixture_roundtrips_with_bytes_loader(name):
    data = (Path(__file__).parent / "fixtures" / name).read_bytes()
    db = load_bytes(data)
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    assert output.getvalue().encode("latin-1") == data


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_each_supported_legacy_version_roundtrips(version):
    data = (Path(__file__).parent / "fixtures" / "Minimal.db").read_bytes()
    data = data.replace(b"Format Version 1", f"Format Version {version}".encode(), 1)
    db = load_bytes(data)
    assert db.version == version
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    assert output.getvalue().encode("latin-1") == data


@pytest.mark.parametrize("version", [0, 5, 16, 18])
def test_unsupported_format_versions_fail_explicitly(version):
    data = database_bytes().replace(b"Format Version 17", f"Format Version {version}".encode(), 1)
    with pytest.raises(ParseError) as error:
        load_bytes(data)
    assert error.value.code == "unsupported_format"


def test_truncated_full_sources_fail_at_every_required_record_boundary():
    lines = database_bytes([object_record(verbs=1)], programs=b"1\n#0:0\nsecret\n.\n").splitlines(keepends=True)
    for boundary in range(len(lines)):
        with pytest.raises(ParseError) as error:
            load_bytes(b"".join(lines[:boundary]))
        assert error.value.code == "unexpected_eof"
