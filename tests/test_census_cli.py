from pathlib import Path

from click.testing import CliRunner

import lambdamoo_db.cli as cli

TOASTCORE = Path(__file__).parent.parent / "toastcore.db"


def run(*args):
    result = CliRunner().invoke(cli.moodb, list(args))
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def test_census_groups_by_owner_with_sizes_on_first_and_later_runs(tmp_path):
    for _ in range(2):  # the first run builds the offset index
        lines = run("--cache-dir", str(tmp_path), "--db", str(TOASTCORE), "census", "--limit", "2")
        assert lines[0] == "127 objects, 2082546 bytes, in 5 groups by owner"
        assert lines[1].split()[:4] == ["55", "1138310", "#36", "Hacker"]


def test_census_sizes_add_up_to_the_object_records_and_programs(tmp_path):
    lines = run("--cache-dir", str(tmp_path), "--db", str(TOASTCORE), "census", "--by", "parent", "--limit", "100")
    assert sum(int(line.split()[1]) for line in lines[1:]) == 2082546
    assert sum(int(line.split()[0]) for line in lines[1:]) == 127


def test_census_since_itself_is_empty(tmp_path):
    lines = run("--cache-dir", str(tmp_path), "--db", str(TOASTCORE), "census", "--since", str(TOASTCORE))
    assert lines[0].startswith("0 objects, 0 bytes, in 0 groups by owner")


def test_census_needs_the_index():
    result = CliRunner().invoke(cli.moodb, ["--no-cache", "--db", str(TOASTCORE), "census"])
    assert result.exit_code == 2
    assert "drop --no-cache" in result.output


def test_grep_verb_and_verbs_without_match():
    with_pass = set(run("--no-cache", "--db", str(TOASTCORE), "grep", "--verb", "recycle", "-l", r"pass\("))
    without = set(run("--no-cache", "--db", str(TOASTCORE), "grep", "--verb", "recycle", "-L", r"pass\("))
    assert with_pass and not with_pass & without
    every = run("--no-cache", "--db", str(TOASTCORE), "grep", "--verb", "recycle", "-L", "a^")  # matches no line
    assert set(every) == with_pass | without
    assert all('"' in line and ":[" in line for line in every)


def test_format_time_takes_any_task_time():
    from lambdamoo_db.inspection import format_time

    assert format_time(0) == "1970-01-01 00:00:00Z"
    assert format_time(-86400) == "1969-12-31 00:00:00Z"
    assert format_time(1792279400) == "2026-10-17 23:23:20Z"
    assert format_time(9223372036854775807) == "9223372036854775807s"  # suspended without a deadline
    assert format_time(None) == "-"


def test_slots_missing_by_groups():
    by_property = run("--no-cache", "--db", str(TOASTCORE), "slots", "--missing-by", "property")
    assert by_property[0] == "5 slots name a missing object, in 2 groups by property"
    assert by_property[1].split() == ["4", ".entrances"]
    by_holder = run("--no-cache", "--db", str(TOASTCORE), "slots", "--missing-by", "holder", "--limit", "1")
    assert by_holder[0] == "5 slots name a missing object, in 5 groups by holder"
    assert len(by_holder) == 2
