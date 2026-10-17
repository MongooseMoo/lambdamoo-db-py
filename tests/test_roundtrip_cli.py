from pathlib import Path

import pytest
from click.testing import CliRunner

import lambdamoo_db.cli as cli

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).parent.parent


@pytest.mark.parametrize("path", [FIXTURES / "Minimal.db", FIXTURES / "Suspended.db", FIXTURES / "TypedMapKeys.db",
                                  PROJECT_ROOT / "toastcore.db", PROJECT_ROOT / "toast2.db"],
                         ids=lambda p: p.name)
def test_roundtrip_reports_identical(path):
    result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(path), "roundtrip"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("identical: ")


def test_roundtrip_reports_line_numbers_never_text(tmp_path):
    data = (FIXTURES / "Suspended.db").read_bytes()
    marker = b"\n#0\n"
    name_at = data.index(marker) + len(marker)
    name_end = data.index(b"\n", name_at)
    at = data.index(b"\n", name_end + 1) + 1  # past the name and the empty legacy line
    end = data.index(b"\n", at)
    number = data[at:end]
    assert number.isdigit()
    # A leading zero on the object's flags: read as the same integer, written without it.
    changed = tmp_path / "changed.db"
    changed.write_bytes(data[:at] + b"0" + data[at:])
    line = data[:at].count(b"\n") + 1
    result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(changed), "roundtrip"])
    assert result.exit_code == 1, result.output
    assert f"line {line}: original {len(number) + 1} bytes, written {len(number)} bytes, after header #0" in result.output
    assert data[name_at:name_end].decode("latin-1") not in result.output

    shown = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(changed), "roundtrip", "--show", "1"])
    assert f"original from line {line}:\n  0{number.decode()}\nwritten from line {line}:\n  {number.decode()}\n" in shown.output
