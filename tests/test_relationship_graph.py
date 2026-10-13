import hashlib
import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import Anon, CLEAR, MooDatabase, MooError, MooObject, ObjNum, Property, Waif, WaifReference
from lambdamoo_db.lazy import LazyDatabase, build_index, open_indexed
from lambdamoo_db.reader import load
from lambdamoo_db.relationship_graph import Node, RelationshipGraph, parse_node
from lambdamoo_db.writer import dump


FIXTURE = Path(__file__).parent / "fixtures" / "TypedMapKeys.db"
DIGEST = "a" * 64


def obj(db, oid, values=(), parents=(), anon=False):
    value = MooObject(oid, "secret object name", 0, 1, -1, list(parents), anon=anon)
    value.propdefs_count = len(values)
    value.properties = [Property("secret-property", item, 1, 0) for item in values]
    db.objects[oid] = value
    return value


def world():
    db = MooDatabase()
    obj(db, 1, [[ObjNum(2), ObjNum(3), 2, "#4", MooError(4)]])
    obj(db, 2, [ObjNum(4)])
    obj(db, 3, [ObjNum(4)])
    obj(db, 4, [ObjNum(1)])
    obj(db, 5)
    return db


def overlay(edges=(), coverage=()):
    return {"version": 1, "snapshot_sha256": DIGEST, "source": "captured-registration",
            "edges": list(edges), "coverage": list(coverage)}


def edge(source="#1", target="#5", kind="registered"):
    return {"source": source, "target": target, "kind": kind, "evidence": ["registration:7"]}


def requirement(mode="path", source="#1", target="#5", kinds=("property",)):
    return {"version": 1, "expectations": [{"id": "connection", "source": source, "target": target,
                                           "kinds": list(kinds), "mode": mode}]}


def test_typed_nested_map_keys_lists_and_privacy():
    db = world()
    db.objects[1].properties[0].value = {ObjNum(2): [ObjNum(3), Anon(6)], "secret map key": ObjNum(4), 2: 4}
    obj(db, 6, anon=True)
    graph = RelationshipGraph(db)
    edges = graph.outgoing(Node("object", 1), ("property",))
    assert [(str(e.target), e.evidence) for e in edges] == [
        ("#2", ("property-slot:0", "map:1:key")),
        ("#3", ("property-slot:0", "map:1:value", "list:1")),
        ("anonymous:6", ("property-slot:0", "map:1:value", "list:2")),
        ("#4", ("property-slot:0", "map:2:value")),
    ]
    assert "secret" not in json.dumps([e.to_dict() for e in edges])
    labels = graph.labels(edges[0].to_dict())
    assert labels["property_name"] == "secret-property"
    assert "secret map key" not in json.dumps(labels)


def test_structural_kinds_are_distinct_and_do_not_infer_reciprocals():
    db = world()
    db.objects[1].parents = [ObjNum(2)]
    db.objects[1].children = [ObjNum(3)]
    db.objects[1].contents = [ObjNum(4)]
    graph = RelationshipGraph(db)
    assert [(e.kind, e.target.id) for e in graph.outgoing(Node("object", 1)) if e.kind != "property"] == [
        ("owner", 1), ("location", -1), ("parent", 2), ("child", 3), ("content", 4)]
    assert not graph.outgoing(Node("object", 2), ("child",))


def test_cycle_shared_descendants_and_shortest_directed_path():
    graph = RelationshipGraph(world())
    result = graph.traverse(Node("object", 1), target=Node("object", 4), kinds=["property"], max_depth=10)
    assert result["status"] == "present"
    assert len(result["path"]) == 2
    assert [e["source"]["id"] for e in result["path"]] == [1, 2]
    result = graph.traverse(Node("object", 1), kinds=["property"], max_depth=10)
    assert result["status"] == "complete"
    assert len(result["nodes"]) == 4 and len(result["edges"]) == 5
    assert graph.traverse(Node("object", 5), target=Node("object", 1), kinds=["property"])["status"] == "missing"


@pytest.mark.parametrize("bounds,reason", [({"max_depth": 1}, "max_depth"), ({"max_nodes": 2}, "max_nodes"),
                                         ({"max_edges": 1}, "max_edges"), ({"max_depth": 0}, "max_depth")])
def test_bounds_never_report_missing(bounds, reason):
    result = RelationshipGraph(world()).traverse(Node("object", 1), target=Node("object", 4), kinds=["property"], **bounds)
    assert result["status"] == "unknown"
    assert reason in result["truncated"]


def test_zero_length_path_and_empty_complete_neighborhood():
    graph = RelationshipGraph(world())
    result = graph.traverse(Node("object", 1), target=Node("object", 1), kinds=["property"], max_depth=0)
    assert result["status"] == "present" and result["path"] == []
    result = graph.traverse(Node("object", 5), kinds=["property"], max_depth=0)
    assert result["status"] == "complete"


def test_depth_boundary_preserves_back_edges_to_known_nodes():
    db = world()
    db.objects[1].properties[0].value = ObjNum(1)
    result = RelationshipGraph(db).traverse(Node("object", 1), kinds=["property"], max_depth=0)
    assert result["status"] == "complete" and len(result["edges"]) == 1
    db.objects[1].properties[0].value = ObjNum(2)
    db.objects[2].properties[0].value = ObjNum(1)
    result = RelationshipGraph(db).traverse(Node("object", 1), kinds=["property"], max_depth=1)
    assert result["status"] == "complete" and len(result["edges"]) == 2


@pytest.mark.parametrize("node", [Node("object", 999), Node("object", -1), Node("waif", 999)])
def test_missing_special_identity_is_not_reflexively_present(node):
    result = RelationshipGraph(world()).traverse(node, target=node)
    assert result["status"] == "unknown" and result["path"] == []


@pytest.mark.parametrize("bounds", [{"max_nodes": True}, {"max_depth": 1.5}, {"max_edges": -1}])
def test_api_rejects_invalid_bounds(bounds):
    with pytest.raises(ValueError):
        RelationshipGraph(world()).traverse(Node("object", 1), **bounds)


def test_incoming_reverse_built_once_and_filter_generators():
    graph = RelationshipGraph(world())
    assert [e.source.id for e in graph.incoming(Node("object", 4), (k for k in ["property"]))] == [2, 3]
    assert len(graph.incoming(Node("object", 4), ["property"])) == 2
    assert graph.stats == {"nodes_indexed": 5, "reverse_builds": 1}
    assert len(graph.outgoing(Node("object", 1), (k for k in ["property"]))) == 2


def test_waifs_are_nodes_and_cycles_are_finite():
    db = world()
    db.objects[1].properties[0].value = [WaifReference(0), WaifReference(0)]
    db.waifs[0] = Waif(ObjNum(2), ObjNum(3), [(0, [WaifReference(1), ObjNum(5)])], 1)
    db.waifs[1] = Waif(ObjNum(2), ObjNum(3), [(0, WaifReference(0))], 1)
    graph = RelationshipGraph(db)
    result = graph.traverse(Node("object", 1), target=Node("object", 5), kinds=["property", "waif-property"], max_depth=4)
    assert [e["target"]["type"] for e in result["path"]] == ["waif", "object"]
    assert result["path"][1]["evidence"] == ["waif-slot:0", "list:2"]
    neighborhood = graph.traverse(Node("object", 1), kinds=["property", "waif-property"], max_depth=10)
    assert len(neighborhood["nodes"]) == 4 and len(neighborhood["edges"]) == 5
    assert [e.kind for e in graph.outgoing(Node("waif", 0), ["waif-owner", "waif-class"])] == ["waif-class", "waif-owner"]


def test_dangling_special_negative_and_type_mismatch_are_separate():
    db = world()
    obj(db, 6, anon=True)
    db.objects[1].properties[0].value = [ObjNum(99), ObjNum(-1), ObjNum(-2), ObjNum(-3), ObjNum(-8),
                                       ObjNum(6), Anon(6), WaifReference(404)]
    result = RelationshipGraph(db).diagnostics(["property"])
    assert [e["target"]["id"] for e in result["dangling"]] == [99, 404]
    assert [e["target_status"] for e in result["special"]] == ["special:nothing", "special:ambiguous", "special:failed_match", "special:negative"]
    assert len(result["type_mismatch"]) == 1 and result["type_mismatch"][0]["target"]["id"] == 6


def test_stored_and_effective_clear_holder_provenance():
    db = MooDatabase()
    base = obj(db, 1, [ObjNum(5)])
    left = obj(db, 2, parents=[ObjNum(1)])
    left.properties = [Property(None, ObjNum(4), 1, 0)]
    right = obj(db, 3, parents=[ObjNum(1)])
    right.properties = [Property(None, ObjNum(5), 1, 0)]
    child = obj(db, 4, parents=[ObjNum(2), ObjNum(3)])
    child.properties = [Property(None, CLEAR, 1, 0)]
    obj(db, 5)
    assert RelationshipGraph(db).outgoing(Node("object", 4), ["property"]) == ()
    edges = RelationshipGraph(db, effective=True).outgoing(Node("object", 4), ["property"])
    assert len(edges) == 1 and edges[0].target.id == 4
    assert edges[0].value_from == Node("object", 2) and edges[0].provenance == "effective"
    assert RelationshipGraph(db, effective=True)._layout(4) == ((1, 0),)
    # A clear cycle terminates without fabricating a reference.
    base.properties[0].value = CLEAR
    base.parents = [ObjNum(4)]
    left.properties[0].value = CLEAR
    assert RelationshipGraph(db, effective=True).outgoing(Node("object", 4), ["property"]) == ()


def test_unresolved_effective_clear_prevents_absence_claim():
    db = world()
    db.objects[1].properties[0].value = CLEAR
    db.objects[1].parents = [ObjNum(999)]
    graph = RelationshipGraph(db, effective=True)
    assert not graph.complete(Node("object", 1), ["property"])
    result = graph.audit(requirement(mode="direct"))[0]
    assert result["status"] == "unknown" and not result["coverage_complete"]
    graph.incoming(Node("object", 5), ["property"])
    assert not graph.incoming_complete(["property"])


def test_failed_reverse_build_is_not_cached_as_complete():
    db = world()
    cyclic = []
    cyclic.append(cyclic)
    db.objects[2].properties[0].value = cyclic
    graph = RelationshipGraph(db)
    for _ in range(2):
        with pytest.raises(ValueError, match="cyclic Python"):
            graph.incoming(Node("object", 5), ["property"])
        assert graph._reverse is None and graph.stats["reverse_builds"] == 0


def test_deep_values_are_iterative_and_shared_paths_preserved():
    db = world()
    value = ObjNum(5)
    for _ in range(1200):
        value = [value]
    db.objects[1].properties[0].value = value
    edges = RelationshipGraph(db).outgoing(Node("object", 1), ["property"])
    assert len(edges[0].evidence) == 1201
    shared = [ObjNum(2)]
    db.objects[1].properties[0].value = [shared, shared]
    assert len(RelationshipGraph(db).outgoing(Node("object", 1), ["property"])) == 2
    shared.append(shared)
    with pytest.raises(ValueError, match="cyclic Python"):
        RelationshipGraph(db).outgoing(Node("object", 1))


@pytest.mark.parametrize("value", ["1", "#x", "waif:a", "anonymous:2:3", 1, None])
def test_bad_explicit_nodes(value):
    with pytest.raises(ValueError):
        parse_node(value)


def test_overlay_present_without_complete_coverage():
    graph = RelationshipGraph(world(), overlay=overlay([edge()]), snapshot_sha256=DIGEST)
    result = graph.audit(requirement(kinds=["registered"]))[0]
    assert result["status"] == "present"
    assert result["path"][0]["provenance"] == "external:captured-registration"
    assert not graph.complete(Node("object", 1), ["registered"])
    supplied = edge()
    supplied["evidence"] = ["property-slot:not-an-ordinal"]
    graph = RelationshipGraph(world(), overlay=overlay([supplied]), snapshot_sha256=DIGEST)
    labels = graph.labels(graph.outgoing(Node("object", 1), ["registered"])[0].to_dict())
    assert "property_name" not in labels


def test_absence_unknown_until_source_kind_covered():
    incomplete = RelationshipGraph(world(), overlay=overlay(), snapshot_sha256=DIGEST)
    assert incomplete.audit(requirement(mode="direct", kinds=["registered"]))[0]["status"] == "unknown"
    scoped = RelationshipGraph(world(), overlay=overlay(coverage=[{"kind": "registered", "sources": ["#1"]}]), snapshot_sha256=DIGEST)
    assert scoped.audit(requirement(mode="direct", kinds=["registered"]))[0]["status"] == "missing"
    assert scoped.audit(requirement(mode="direct", source="#2", kinds=["registered"]))[0]["status"] == "unknown"
    assert not scoped.incoming_complete(["registered"])
    complete = RelationshipGraph(world(), overlay=overlay(coverage=[{"kind": "registered", "sources": "*"}]), snapshot_sha256=DIGEST)
    assert complete.incoming_complete(["registered"])


def test_mixed_path_coverage_and_missing_endpoint():
    graph = RelationshipGraph(world(), overlay=overlay([edge(source="#4", target="#5")], [{"kind": "registered", "sources": ["#4"]}]), snapshot_sha256=DIGEST)
    assert graph.audit(requirement(kinds=["property", "registered"]), max_depth=10)[0]["status"] == "present"
    assert graph.audit(requirement(target="#999"))[0]["status"] == "unknown"
    assert graph.audit(requirement(source="#5", target="#1", kinds=["registered"]))[0]["status"] == "unknown"


def test_direct_self_requires_edge_not_reflexive_path():
    graph = RelationshipGraph(world())
    assert graph.audit(requirement(mode="direct", source="#5", target="#5"))[0]["status"] == "missing"
    assert graph.audit(requirement(mode="path", source="#5", target="#5"))[0]["status"] == "present"
    with pytest.raises(ValueError):
        graph.audit(requirement(mode="direct"), max_edges=-1)


@pytest.mark.parametrize("mutation", [
    lambda o: o.update(snapshot_sha256="b" * 64),
    lambda o: o.update(version=2),
    lambda o: o.update(extra=True),
    lambda o: o.update(edges="bad"),
    lambda o: o["edges"][0].update(kind="property"),
    lambda o: o["edges"][0].update(source="#999"),
    lambda o: o["edges"][0].update(evidence=[]),
    lambda o: o["edges"][0].update(target=5),
    lambda o: o.update(coverage=[{"kind": "registered", "sources": ["#999"]}]),
    lambda o: o.update(coverage=[{"kind": "owner", "sources": "*"}]),
    lambda o: o.update(coverage=[{"kind": "registered", "sources": "*"}] * 2),
])
def test_invalid_overlay_fails_closed(mutation):
    payload = overlay([edge()])
    mutation(payload)
    with pytest.raises(ValueError):
        RelationshipGraph(world(), overlay=payload, snapshot_sha256=DIGEST)


@pytest.mark.parametrize("mutation", [lambda r: r.update(version=2), lambda r: r["expectations"][0].update(kinds=[]),
                                     lambda r: r["expectations"][0].update(mode="dispatch"),
                                     lambda r: r["expectations"][0].update(extra=True)])
def test_invalid_expectations(mutation):
    payload = requirement()
    mutation(payload)
    with pytest.raises(ValueError):
        RelationshipGraph(world()).audit(payload)


def test_lazy_graph_avoids_programs_and_normal_inspection_remains_complete(tmp_path, monkeypatch):
    index, full = build_index(FIXTURE)
    lazy = LazyDatabase(FIXTURE, index)
    graph = RelationshipGraph(lazy)
    with monkeypatch.context() as guard:
        guard.setattr(lazy, "_read_programs", lambda obj: pytest.fail("graph read program"))
        guard.setattr(lazy, "load_all", lambda: pytest.fail("graph full load"))
        graph.incoming(Node("object", 7), ["property"])
        graph.diagnostics()
    assert not lazy.objects.loaded
    assert len(lazy._relationship_objects) == len(lazy.objects)
    assert dict(lazy.objects) == full.objects
    assert dict(lazy.waifs) == full.waifs
    # Graph after ordinary inspection gives the same evidence.
    assert graph.diagnostics() == RelationshipGraph(lazy).diagnostics()


def test_lazy_outgoing_does_not_decode_referenced_objects(tmp_path, monkeypatch):
    index, _ = build_index(FIXTURE)
    lazy = LazyDatabase(FIXTURE, index)
    graph = RelationshipGraph(lazy)
    graph.outgoing(Node("object", 0))
    assert list(lazy._relationship_objects) == [0]
    for oid in lazy.objects:
        graph.status(graph.object_node(oid))
    assert list(lazy._relationship_objects) == [0] and not lazy.objects.loaded


def test_lazy_waifs_cross_record_and_normal_load(tmp_path, monkeypatch):
    db = load(str(FIXTURE))
    db.waifs[0] = Waif(ObjNum(7), ObjNum(3), [(0, ObjNum(3))], 1)
    db.objects[0].properties[0].value = WaifReference(0)
    db.objects[2].properties.append(Property("held", WaifReference(0), ObjNum(3), 0))
    db.objects[2].propdefs_count = 1
    path = tmp_path / "waifs.db"
    with path.open("w", encoding="latin-1", newline="") as stream:
        dump(db, stream)
    index, full = build_index(path)
    lazy = LazyDatabase(path, index)
    graph = RelationshipGraph(lazy)
    with monkeypatch.context() as guard:
        guard.setattr(lazy, "_read_programs", lambda obj: pytest.fail("graph read program"))
        graph.outgoing(Node("object", 2))
        edges = graph.outgoing(Node("waif", 0), ["waif-property"])
    assert edges[0].target == Node("object", 3)
    assert not lazy.objects.loaded
    assert dict(lazy.objects) == full.objects and dict(lazy.waifs) == full.waifs


def run_cli(*args):
    return CliRunner().invoke(moodb, ["--db", str(FIXTURE), "--no-cache", *args])


def test_cli_json_and_batch_index_reuse(monkeypatch):
    result = run_cli("graph", "outgoing", "#0", "--format", "json")
    assert result.exit_code == 0, result.exception
    data = json.loads(result.output)
    assert data["node"] == {"type": "object", "id": 0}
    assert "secret" not in result.output
    builds = []
    original = RelationshipGraph._build_reverse
    def count(self):
        if self._reverse is None:
            builds.append(1)
        return original(self)
    monkeypatch.setattr(RelationshipGraph, "_build_reverse", count)
    result = run_cli("batch", "graph incoming #7 --format json", "graph incoming #3 --format json", "obj #0")
    assert result.exit_code == 0, result.exception
    assert len(builds) == 1
    assert "=== obj #0" in result.output
    assert run_cli("batch", "obj #0", "graph outgoing #0").exit_code == 0


def test_cli_snapshot_overlay_audit_and_rejection(tmp_path):
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    payload = overlay(coverage=[{"kind": "registered", "sources": "*"}])
    payload["snapshot_sha256"] = digest
    overlay_path = tmp_path / "overlay.json"
    overlay_path.write_text(json.dumps(payload), encoding="utf-8")
    expectations = tmp_path / "expectations.json"
    expectations.write_text(json.dumps(requirement(source="#0", target="#3", kinds=["registered"])), encoding="utf-8")
    result = run_cli("graph", "fingerprint", "--format", "json")
    assert json.loads(result.output)["snapshot_sha256"] == digest
    result = run_cli("graph", "--overlay", str(overlay_path), "audit", str(expectations), "--format", "json")
    assert result.exit_code == 1 and json.loads(result.output)["expectations"][0]["status"] == "missing"
    payload["coverage"] = []
    overlay_path.write_text(json.dumps(payload), encoding="utf-8")
    result = run_cli("graph", "--overlay", str(overlay_path), "audit", str(expectations), "--format", "json")
    assert result.exit_code == 3
    payload["snapshot_sha256"] = DIGEST
    overlay_path.write_text(json.dumps(payload), encoding="utf-8")
    result = run_cli("graph", "--overlay", str(overlay_path), "outgoing", "#0")
    assert result.exit_code == 1 and "SHA256" in result.output


def test_cli_indexed_matches_full_and_narrow_default(tmp_path):
    open_indexed(FIXTURE, tmp_path)
    args = ["graph", "neighborhood", "#0", "--max-depth", "2", "--format", "json"]
    full = run_cli(*args)
    indexed = CliRunner().invoke(moodb, ["--db", str(FIXTURE), "--cache-dir", str(tmp_path), *args])
    assert indexed.exit_code == full.exit_code == 0
    assert indexed.output == full.output
    assert json.loads(full.output)["kinds"] == ["property", "waif-property"]
    named = run_cli("graph", "--labels", "outgoing", "#0", "--format", "json")
    assert named.exit_code == 0
    assert all("labels" in edge for edge in json.loads(named.output)["edges"])


def test_cli_bounds_and_external_json_errors(tmp_path):
    result = run_cli("graph", "neighborhood", "#0", "--max-nodes", "0")
    assert result.exit_code == 2
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{secret value", encoding="utf-8")
    result = run_cli("graph", "--overlay", str(invalid), "outgoing", "#0")
    assert result.exit_code == 1 and "secret value" not in result.output


@pytest.mark.parametrize("kind", ["overlay", "expectations"])
def test_cli_rejects_duplicate_json_fields(tmp_path, kind):
    payload = overlay() if kind == "overlay" else requirement(source="#0", target="#3")
    payload["snapshot_sha256"] = hashlib.sha256(FIXTURE.read_bytes()).hexdigest() if kind == "overlay" else None
    if kind == "expectations":
        del payload["snapshot_sha256"]
    encoded = json.dumps(payload).replace('"version": 1', '"version": 1, "version": 1')
    path = tmp_path / "duplicates.json"
    path.write_text(encoded, encoding="utf-8")
    args = ["graph", "--overlay", str(path), "outgoing", "#0"] if kind == "overlay" else ["graph", "audit", str(path)]
    result = run_cli(*args)
    assert result.exit_code == 1 and "cannot read valid UTF-8 JSON" in result.output


@pytest.mark.parametrize("command", [
    ["outgoing", "#0"], ["incoming", "#3"], ["path", "#0", "#3"],
    ["neighborhood", "#0"], ["dangling"],
])
def test_text_graph_escapes_terminal_controls_even_when_color_enabled(tmp_path, command):
    payload = overlay([edge(source="#0", target="#3"), edge(source="#0", target="#999")])
    payload["source"] = "producer\x1b[31m\x07\u202e"
    payload["edges"][0]["evidence"] = ["reason\x1b[31m\r\x9b"]
    payload["edges"][1]["evidence"] = payload["edges"][0]["evidence"]
    payload["snapshot_sha256"] = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    path = tmp_path / "controls.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = CliRunner().invoke(moodb, ["--db", str(FIXTURE), "--no-cache", "graph", "--overlay", str(path),
                                       "--labels", *command, "--kind", "registered"], color=True)
    assert result.exit_code == 0, result.exception
    assert not any(c in result.output for c in ("\x1b", "\x07", "\r", "\x9b", "\u202e"))
    assert "reason\\u001b[31m\\u000d\\u009b" in result.output


def test_text_audit_id_controls_are_escaped_and_json_preserves_evidence(tmp_path):
    payload = requirement(source="#0", target="#3")
    payload["expectations"][0]["id"] = "expectation\x1b[31m\n\r\x07\u202e"
    path = tmp_path / "id-controls.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    args = ["--db", str(FIXTURE), "--no-cache", "graph", "audit", str(path)]
    text_result = CliRunner().invoke(moodb, args, color=True)
    assert text_result.exit_code in (0, 1)
    assert "expectation\\u001b[31m\\u000a\\u000d\\u0007\\u202e" in text_result.output
    assert not any(c in text_result.output for c in ("\x1b", "\x07", "\r", "\u202e"))
    json_result = CliRunner().invoke(moodb, args + ["--format", "json"], color=True)
    assert json.loads(json_result.output)["expectations"][0]["id"] == payload["expectations"][0]["id"]
    assert "\x1b" not in json_result.output
