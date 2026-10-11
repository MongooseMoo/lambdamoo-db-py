"""Tree measurements are reusable CLI output, including damaged graphs."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lambdamoo_db import cli
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum


def graph_db(edges, relation):
    db = MooDatabase()
    db.objects[0] = MooObject(0, "root", 0, 0, -1)
    for number, links in edges.items():
        obj = MooObject(number, "name must not appear in summaries", 0, 0, -1)
        setattr(obj, relation, [ObjNum(link) for link in links])
        db.objects[number] = obj
    return db


def invoke_graph(monkeypatch, tmp_path, edges, relation, *arguments):
    db = graph_db(edges, relation)
    monkeypatch.setattr(cli, "open_indexed", lambda *_: db)
    dump = tmp_path / "world.db"
    dump.touch()
    return CliRunner().invoke(cli.moodb, ["--db", str(dump), relation, "#1", *arguments])


@pytest.mark.parametrize("relation", ["children", "contents"])
def test_summary_measures_the_whole_tree_without_recursive_flag(monkeypatch, tmp_path, relation):
    result = invoke_graph(monkeypatch, tmp_path, {1: [2, 3], 2: [4, 5], 3: [], 4: [6], 5: [], 6: []},
                          relation, "--summary", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "root": 1, "relation": relation, "direct_count": 2, "descendant_count": 5,
        "leaf_count": 3, "max_depth": 3, "missing_reference_count": 0, "repeated_reference_count": 0,
    }
    assert "name must not appear" not in result.output


@pytest.mark.parametrize("relation", ["children", "contents"])
def test_summary_empty_root_is_not_its_own_descendant_or_leaf(monkeypatch, tmp_path, relation):
    result = invoke_graph(monkeypatch, tmp_path, {1: []}, relation, "--summary", "--format", "json")
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert all(summary[key] == 0 for key in (
        "direct_count", "descendant_count", "leaf_count", "max_depth", "missing_reference_count", "repeated_reference_count",
    ))


@pytest.mark.parametrize("relation", ["children", "contents"])
def test_summary_exposes_missing_edges_duplicates_shared_nodes_and_cycles(monkeypatch, tmp_path, relation):
    edges = {1: [2, 2, 3, 99, 1], 2: [4, 99], 3: [4], 4: [1]}
    result = invoke_graph(monkeypatch, tmp_path, edges, relation, "--summary", "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "root": 1, "relation": relation, "direct_count": 2, "descendant_count": 3,
        "leaf_count": 0, "max_depth": 2, "missing_reference_count": 2, "repeated_reference_count": 4,
    }


def test_summary_depth_uses_shortest_paths_through_multiple_parents(monkeypatch, tmp_path):
    # #6 is reachable at depth two and through a longer branch at depth four.
    edges = {1: [2, 3], 2: [4], 3: [6], 4: [5], 5: [6], 6: [7], 7: []}
    result = invoke_graph(monkeypatch, tmp_path, edges, "children", "--summary", "--format", "json")
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["max_depth"] == 3
    assert summary["descendant_count"] == 6
    assert summary["leaf_count"] == 1
    assert summary["repeated_reference_count"] == 1


def test_summary_does_not_count_a_broken_branch_as_a_leaf(monkeypatch, tmp_path):
    result = invoke_graph(monkeypatch, tmp_path, {1: [2], 2: [99]}, "contents", "--summary", "--format", "json")
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["descendant_count"] == 1
    assert summary["leaf_count"] == 0
    assert summary["missing_reference_count"] == 1


def test_summary_handles_depth_beyond_python_recursion_limit(monkeypatch, tmp_path):
    edges = {number: [number + 1] for number in range(1, 1500)}
    edges[1500] = []
    result = invoke_graph(monkeypatch, tmp_path, edges, "contents", "--summary", "--format", "json")
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["max_depth"] == summary["descendant_count"] == 1499
    assert summary["leaf_count"] == 1


def test_summary_text_is_labelled_and_recursive_flag_is_redundant(monkeypatch, tmp_path):
    result = invoke_graph(monkeypatch, tmp_path, {1: [2], 2: [3], 3: []}, "contents", "--summary", "-r")
    assert result.exit_code == 0, result.output
    assert result.output == (
        "#1 contents\n"
        "  direct_count 1\n"
        "  descendant_count 2\n"
        "  leaf_count 1\n"
        "  max_depth 2\n"
        "  missing_reference_count 0\n"
        "  repeated_reference_count 0\n"
    )


def test_json_requires_summary(monkeypatch, tmp_path):
    result = invoke_graph(monkeypatch, tmp_path, {1: []}, "contents", "--format", "json")
    assert result.exit_code == 2
    assert "--format json requires --summary" in result.output


def test_summary_preserves_missing_root_error(monkeypatch, tmp_path):
    result = invoke_graph(monkeypatch, tmp_path, {}, "contents", "--summary")
    assert result.exit_code == 1
    assert "#1 does not exist" in result.output


def test_summary_batch_reuses_database(monkeypatch, tmp_path):
    db = graph_db({1: [2], 2: []}, "contents")
    loads = []

    def open_database(*args):
        loads.append(args)
        return db

    monkeypatch.setattr(cli, "open_indexed", open_database)
    dump = tmp_path / "world.db"
    dump.touch()
    result = CliRunner().invoke(cli.moodb, ["--db", str(dump), "batch", "contents #1 --summary", "contents #2 --summary"])
    assert result.exit_code == 0, result.output
    assert len(loads) == 1
    assert "descendant_count 1" in result.output
    assert "descendant_count 0" in result.output


@pytest.mark.parametrize("relation", ["children", "contents"])
def test_real_dump_summary_matches_indexed_and_full_readers(tmp_path, relation):
    dump = Path(__file__).parent.parent / "toastcore.db"
    runner = CliRunner()
    query = [relation, "#1", "--summary", "--format", "json"]
    indexed = runner.invoke(cli.moodb, ["--db", str(dump), "--cache-dir", str(tmp_path), *query])
    full = runner.invoke(cli.moodb, ["--db", str(dump), "--no-cache", *query])
    assert indexed.exit_code == full.exit_code == 0, (indexed.output, full.output)
    assert json.loads(indexed.output) == json.loads(full.output)
