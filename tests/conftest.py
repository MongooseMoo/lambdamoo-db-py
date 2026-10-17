"""
Pytest configuration and fixtures for lambdamoo-db tests.

Key fixtures provide:
- Paths to toaststunt reference databases (v17 format)
- Database loading utilities
- Line-by-line comparison for round-trip testing
"""
import os
from pathlib import Path
from io import StringIO

import pytest

# Resolve paths relative to this file
TESTS_DIR = Path(__file__).parent
PROJECT_ROOT = TESTS_DIR.parent
TOASTSTUNT_DIR = PROJECT_ROOT.parent / "toaststunt"


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "slow: marks tests as slow (run with --slow)")


def pytest_collection_modifyitems(config, items):
    """Skip slow tests unless --slow is passed."""
    if config.getoption("--slow", default=False):
        return
    skip_slow = pytest.mark.skip(reason="need --slow option to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


def pytest_addoption(parser):
    """Add --slow option to pytest."""
    parser.addoption(
        "--slow", action="store_true", default=False, help="run slow tests"
    )


# =============================================================================
# Path Fixtures
# =============================================================================

@pytest.fixture
def toaststunt_dir() -> Path:
    """Path to the toaststunt directory containing reference DBs."""
    if not TOASTSTUNT_DIR.exists():
        pytest.skip(f"toaststunt directory not found at {TOASTSTUNT_DIR}")
    return TOASTSTUNT_DIR


@pytest.fixture
def toastcore_db_path() -> Path:
    """Path to the v17 toastcore.db written by ToastStunt, tracked in this repository."""
    return PROJECT_ROOT / "toastcore.db"


@pytest.fixture
def mongoose_db_path(toaststunt_dir) -> Path:
    """Path to toaststunt's v17 mongoose.db (large, 35MB)."""
    path = toaststunt_dir / "mongoose.db"
    if not path.exists():
        pytest.skip(f"mongoose.db not found at {path}")
    return path


@pytest.fixture
def minimal_db_path(toaststunt_dir) -> Path:
    """Path to toaststunt's minimal v1 database."""
    path = toaststunt_dir / "Minimal.db"
    if not path.exists():
        pytest.skip(f"Minimal.db not found at {path}")
    return path


@pytest.fixture
def temp_db_path(tmp_path) -> Path:
    """Temporary path for write tests."""
    return tmp_path / "test_output.db"


# =============================================================================
# Database Loading Fixtures
# =============================================================================

@pytest.fixture
def toastcore_db(toastcore_db_path):
    """Load toaststunt's v17 toastcore.db as a MooDatabase."""
    from lambdamoo_db.reader import load
    return load(str(toastcore_db_path))


# =============================================================================
# Comparison Utilities
# =============================================================================

class LineDiff:
    """Represents a difference between two lines."""
    def __init__(self, line_num: int, expected: str, actual: str):
        self.line_num = line_num
        self.expected = expected
        self.actual = actual

    def __repr__(self):
        return f"Line {self.line_num}:\n  expected: {self.expected!r}\n  actual:   {self.actual!r}"


def compare_files_line_by_line(
    expected_path: Path | str,
    actual_path: Path | str,
    max_diffs: int = 10,
    normalize: bool = True
) -> list[LineDiff]:
    """
    Compare two files line by line.

    Args:
        expected_path: Path to the reference file
        actual_path: Path to the file being tested
        max_diffs: Maximum number of differences to return
        normalize: If True, strip trailing whitespace from each line

    Returns:
        List of LineDiff objects describing differences (empty if files match)
    """
    diffs = []

    with open(expected_path, "r", encoding="latin-1") as expected_file, \
         open(actual_path, "r", encoding="latin-1") as actual_file:

        line_num = 0
        while True:
            line_num += 1
            expected_line = expected_file.readline()
            actual_line = actual_file.readline()

            # Both files ended
            if not expected_line and not actual_line:
                break

            # Normalize if requested
            if normalize:
                expected_line = expected_line.rstrip()
                actual_line = actual_line.rstrip()

            if expected_line != actual_line:
                diffs.append(LineDiff(line_num, expected_line, actual_line))
                if len(diffs) >= max_diffs:
                    break

    return diffs


def compare_db_output_to_reference(
    db,
    reference_path: Path | str,
    max_diffs: int = 10
) -> list[LineDiff]:
    """
    Write a database to a string and compare to reference file.

    Args:
        db: MooDatabase to serialize
        reference_path: Path to the reference file to compare against
        max_diffs: Maximum number of differences to return

    Returns:
        List of LineDiff objects (empty if output matches reference)
    """
    from lambdamoo_db.writer import Writer

    output = StringIO()
    writer = Writer(db=db, output_file=output)
    writer.writeDatabase()
    output.seek(0)

    diffs = []
    with open(reference_path, "r", encoding="latin-1") as ref_file:
        line_num = 0
        while True:
            line_num += 1
            expected_line = ref_file.readline()
            actual_line = output.readline()

            if not expected_line and not actual_line:
                break

            expected_line = expected_line.rstrip()
            actual_line = actual_line.rstrip()

            if expected_line != actual_line:
                diffs.append(LineDiff(line_num, expected_line, actual_line))
                if len(diffs) >= max_diffs:
                    break

    return diffs


@pytest.fixture
def compare_files():
    """Fixture providing the compare_files_line_by_line function."""
    return compare_files_line_by_line


@pytest.fixture
def compare_db_to_reference():
    """Fixture providing the compare_db_output_to_reference function."""
    return compare_db_output_to_reference
