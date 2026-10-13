"""Evidence-bearing, read-only relationships in a single immutable snapshot.

Node identities preserve MOO types. Evidence paths use ordinals rather than
stored names or map keys, so graph output cannot disclose string values.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
import re
from typing import Any, Iterable

from .database import Anon, CLEAR, ObjNum, WaifReference


@dataclass(frozen=True, order=True)
class Node:
    type: str
    id: int

    def __post_init__(self):
        if self.type not in ("object", "anonymous", "waif") or type(self.id) is not int:
            raise ValueError("node requires an object, anonymous or waif type and integer id")

    def __str__(self) -> str:
        return f"#{self.id}" if self.type == "object" else f"{self.type}:{self.id}"


@dataclass(frozen=True)
class Edge:
    source: Node
    target: Node
    kind: str
    evidence: tuple[str, ...]
    provenance: str = "stored"
    value_from: Node | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["evidence"] = list(self.evidence)
        return result


BUILTIN_KINDS = frozenset({"owner", "location", "parent", "child", "content",
                           "property", "waif-class", "waif-owner", "waif-property"})
SPECIAL_NEGATIVES = {-1: "nothing", -2: "ambiguous", -3: "failed_match"}
_KIND = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,99}\Z")


def parse_node(value: str) -> Node:
    """Parse an explicit identity without interpreting strings as expressions."""
    if not isinstance(value, str):
        raise ValueError("node must be an explicit string identity")
    match = re.fullmatch(r"(?:(#|object:|anonymous:|waif:))(-?\d+)", value)
    if not match:
        raise ValueError("node must be #N, object:N, anonymous:N or waif:N")
    return Node({"#": "object", "object:": "object", "anonymous:": "anonymous", "waif:": "waif"}[match[1]], int(match[2]))


def _validate_bounds(max_depth: int = 3, max_nodes: int = 100, max_edges: int = 1000) -> None:
    if any(type(value) is not int for value in (max_depth, max_nodes, max_edges)) or min(max_depth, max_nodes, max_edges) < 0 or max_nodes < 1:
        raise ValueError("bounds must be nonnegative integers; max_nodes must be positive")


class RelationshipGraph:
    """On-demand forward index and one-time reverse index; no verb programs.

    Callers must treat the database as immutable for this index's lifetime.
    ``effective`` follows clear slots, marking their holder separately; the
    default describes stored references, without inherited duplicates.
    """

    def __init__(self, db, *, effective: bool = False, overlay: dict | None = None,
                 snapshot_sha256: str | None = None):
        self.db = db
        self.effective = effective
        self._outgoing: dict[Node, tuple[Edge, ...]] = {}
        self._reverse: dict[Node, list[Edge]] | None = None
        self._layouts: dict[int, tuple[tuple[int, int], ...]] = {}
        self._external: dict[Node, list[Edge]] = defaultdict(list)
        self._coverage: dict[str, set[Node] | None] = {}
        self._unresolved_effective: set[Node] = set()
        self.kinds = set(BUILTIN_KINDS)
        self.stats = {"nodes_indexed": 0, "reverse_builds": 0}
        if overlay is not None:
            self._load_overlay(overlay, snapshot_sha256)

    def status(self, node: Node) -> str:
        if node.type in ("object", "anonymous") and node.id < 0:
            return "special:" + SPECIAL_NEGATIVES.get(node.id, "negative")
        if node.type == "waif":
            return "existing" if node.id in self.db.waifs else "dangling"
        if hasattr(self.db, "index"):
            record = self.db.index.records.get(node.id)
            if record is None:
                return "dangling"
            return "existing" if record[2] == (node.type == "anonymous") else "type_mismatch"
        obj = self._object(node.id)
        if obj is None:
            return "dangling"
        return "existing" if obj.anon == (node.type == "anonymous") else "type_mismatch"

    def object_node(self, oid: int) -> Node:
        if hasattr(self.db, "index"):
            record = self.db.index.records.get(int(oid))
            return Node("anonymous" if record is not None and record[2] else "object", int(oid))
        obj = self._object(int(oid))
        return Node("anonymous" if obj is not None and obj.anon else "object", int(oid))

    def _object(self, oid: int):
        if hasattr(self.db, "relationship_object"):
            return self.db.relationship_object(oid)
        return self.db.objects.get(oid)

    def _waif(self, wid: int):
        if hasattr(self.db, "relationship_waif"):
            return self.db.relationship_waif(wid)
        return self.db.waifs.get(wid)

    def labels(self, edge: dict) -> dict:
        """Opt-in names for an edge; never property values or map-key text."""
        result = {}
        source, target = Node(**edge["source"]), Node(**edge["target"])
        for role, node in (("source", source), ("target", target)):
            if node.type != "waif" and self.status(node) == "existing":
                result[role + "_name"] = self._object(node.id).name
        evidence = edge["evidence"]
        if edge["kind"] == "property" and evidence and source.type != "waif" and evidence[0].startswith("property-slot:"):
            index = int(evidence[0].split(":")[1])
            layout = self._layout(source.id)
            if index < len(layout):
                definer, own_slot = layout[index]
                result["property_name"] = self._object(definer).properties[own_slot].propertyName
                result["property_definer"] = asdict(self.object_node(definer))
        elif edge["kind"] == "waif-property" and evidence and source.type == "waif" and evidence[0].startswith("waif-slot:"):
            waif = self._waif(source.id)
            if waif is not None:
                names = []
                for definer, slot in self._layout(int(waif.waif_class)):
                    name = self._object(definer).properties[slot].propertyName
                    if isinstance(name, str) and name.startswith(":"):
                        names.append((name[1:], definer))
                index = int(evidence[0].split(":")[1])
                if 0 <= index < len(names):
                    name, definer = names[index]
                    result["property_name"] = name
                    result["property_definer"] = asdict(self.object_node(definer))
        return result

    def _layout(self, oid: int) -> tuple[tuple[int, int], ...]:
        if oid not in self._layouts:
            stack, seen, result = [oid], set(), []
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                obj = self._object(current)
                if obj is None:
                    continue
                count = obj.propdefs_count
                result.extend((current, slot) for slot in range(count))
                stack.extend(int(p) for p in reversed(obj.parents))
            self._layouts[oid] = tuple(result)
        return self._layouts[oid]

    def _effective(self, obj, index: int) -> tuple[Any, Node]:
        layout = self._layout(obj.id)
        holder, seen = obj, set()
        definition = layout[index] if index < len(layout) else None
        while holder.id not in seen:
            seen.add(holder.id)
            current_layout = self._layout(holder.id)
            slot = current_layout.index(definition) if definition in current_layout else index
            if slot >= len(holder.properties):
                return CLEAR, self.object_node(holder.id)
            value = holder.properties[slot].value
            if value is not CLEAR:
                return value, self.object_node(holder.id)
            parent = next((self._object(int(p)) for p in holder.parents
                           if definition in self._layout(int(p))), None)
            if parent is None:
                break
            holder = parent
        return CLEAR, self.object_node(holder.id)

    def _value_edges(self, source: Node, value: Any, path: tuple[str, ...],
                     kind: str, holder: Node) -> Iterable[Edge]:
        stack = [(value, path, frozenset())]
        while stack:
            current, evidence, ancestors = stack.pop()
            if isinstance(current, ObjNum):
                yield Edge(source, Node("object", int(current)), kind, evidence,
                           "effective" if self.effective else "stored", holder)
            elif isinstance(current, Anon):
                yield Edge(source, Node("anonymous", int(current)), kind, evidence,
                           "effective" if self.effective else "stored", holder)
            elif isinstance(current, WaifReference):
                yield Edge(source, Node("waif", current.index), kind, evidence,
                           "effective" if self.effective else "stored", holder)
            elif isinstance(current, list):
                if id(current) in ancestors:
                    raise ValueError("cyclic Python value container is not representable in a textdump")
                nested = ancestors | {id(current)}
                stack.extend((item, evidence + (f"list:{i}",), nested) for i, item in reversed(list(enumerate(current, 1))))
            elif isinstance(current, dict):
                if id(current) in ancestors:
                    raise ValueError("cyclic Python value container is not representable in a textdump")
                nested = ancestors | {id(current)}
                for i, (key, item) in reversed(list(enumerate(current.items(), 1))):
                    stack.append((item, evidence + (f"map:{i}:value",), nested))
                    stack.append((key, evidence + (f"map:{i}:key",), nested))

    def outgoing(self, node: Node, kinds: Iterable[str] | None = None) -> tuple[Edge, ...]:
        kinds = None if kinds is None else frozenset(kinds)
        if node not in self._outgoing:
            edges = []
            if self.status(node) == "existing":
                if node.type == "waif":
                    waif = self._waif(node.id)
                    edges.extend((Edge(node, Node("object", int(waif.waif_class)), "waif-class", ("waif-class",)),
                                  Edge(node, Node("object", int(waif.owner)), "waif-owner", ("waif-owner",))))
                    for slot, value in waif.props:
                        edges.extend(self._value_edges(node, value, (f"waif-slot:{slot}",), "waif-property", node))
                else:
                    obj = self._object(node.id)
                    for kind, targets in (("owner", [obj.owner]), ("location", [obj.location]),
                                          ("parent", obj.parents), ("child", obj.children), ("content", obj.contents)):
                        for i, target in enumerate(targets):
                            edges.append(Edge(node, self.object_node(int(target)), kind, (f"{kind}:{i}",)))
                    for index, prop in enumerate(obj.properties):
                        value, holder = self._effective(obj, index) if self.effective else (prop.value, node)
                        if value is not CLEAR:
                            edges.extend(self._value_edges(node, value, (f"property-slot:{index}",), "property", holder))
                        elif self.effective:
                            self._unresolved_effective.add(node)
            edges.extend(self._external.get(node, ()))
            self._outgoing[node] = tuple(edges)
            if self.status(node) == "existing":
                self.stats["nodes_indexed"] += 1
        result = self._outgoing[node]
        return result if kinds is None else tuple(edge for edge in result if edge.kind in kinds)

    def _build_reverse(self) -> None:
        if self._reverse is not None:
            return
        reverse = defaultdict(list)
        # Mapping.values() on LazyDatabase loads programs. Iterate record IDs.
        if hasattr(self.db, "relationship_objects"):
            nodes = [Node("anonymous" if obj.anon else "object", obj.id) for obj in self.db.relationship_objects()]
        else:
            nodes = [self.object_node(oid) for oid in self.db.objects]
        nodes.extend(Node("waif", wid) for wid in self.db.waifs)
        known = set(nodes)
        nodes.extend(node for node in self._external if node not in known)
        for node in nodes:
            for edge in self.outgoing(node):
                reverse[edge.target].append(edge)
        self._reverse = reverse
        self.stats["reverse_builds"] += 1

    def incoming(self, node: Node, kinds: Iterable[str] | None = None) -> tuple[Edge, ...]:
        kinds = None if kinds is None else frozenset(kinds)
        self._build_reverse()
        edges = tuple(self._reverse.get(node, ()))
        return edges if kinds is None else tuple(edge for edge in edges if edge.kind in kinds)

    def incoming_complete(self, kinds: Iterable[str]) -> bool:
        """Inbound absence requires provider coverage for every source."""
        kinds = frozenset(kinds)
        if self.effective and "property" in kinds:
            self._build_reverse()
        if "property" in kinds and self._unresolved_effective:
            return False
        return all(kind in BUILTIN_KINDS or (kind in self._coverage and self._coverage[kind] is None) for kind in kinds)

    def complete(self, node: Node, kinds: Iterable[str]) -> bool:
        kinds = frozenset(kinds)
        if self.effective and "property" in kinds:
            self.outgoing(node)
        if "property" in kinds and node in self._unresolved_effective:
            return False
        return all(kind in BUILTIN_KINDS or (kind in self._coverage and
                   (self._coverage[kind] is None or node in self._coverage[kind])) for kind in kinds)

    def traverse(self, root: Node, *, target: Node | None = None, direction: str = "out",
                 kinds: Iterable[str] | None = None, max_depth: int = 3,
                 max_nodes: int = 100, max_edges: int = 1000) -> dict:
        """Bounded breadth-first traversal with explicit coverage and bounds."""
        if direction not in ("out", "in", "both"):
            raise ValueError("direction must be out, in or both")
        _validate_bounds(max_depth, max_nodes, max_edges)
        selected = frozenset(self.kinds if kinds is None else kinds)
        seen, queue, edges, previous = {root: 0}, deque([root]), [], {}
        examined, truncated, incomplete = set(), set(), False
        found = root == target and self.status(root) == "existing"
        while queue and not found:
            node = queue.popleft()
            if self.status(node) != "existing":
                continue
            candidates = []
            if direction in ("out", "both"):
                candidates.extend((edge, edge.target) for edge in self.outgoing(node, selected))
                incomplete |= not self.complete(node, selected)
            if direction in ("in", "both"):
                candidates.extend((edge, edge.source) for edge in self.incoming(node, selected))
                # Inbound completeness needs a whole-provider scope.
                incomplete |= not self.incoming_complete(selected)
            for edge, neighbor in candidates:
                if edge in examined:
                    continue
                if seen[node] >= max_depth and neighbor not in seen:
                    truncated.add("max_depth")
                    continue
                if len(edges) >= max_edges:
                    truncated.add("max_edges")
                    break
                if neighbor not in seen and len(seen) >= max_nodes:
                    truncated.add("max_nodes")
                    continue
                examined.add(edge)
                edges.append(edge)
                if neighbor not in seen:
                    seen[neighbor] = seen[node] + 1
                    previous[neighbor] = (node, edge)
                    queue.append(neighbor)
                if neighbor == target:
                    found = True
                    break
            if "max_edges" in truncated:
                break
        path = []
        if found:
            step = target
            while step != root:
                step, edge = previous[step]
                path.append(edge.to_dict())
            path.reverse()
        return {"root": asdict(root), "target": asdict(target) if target else None,
                "direction": direction, "kinds": sorted(selected),
                "nodes": [{**asdict(node), "depth": depth, "status": self.status(node)} for node, depth in seen.items()],
                "edges": [edge.to_dict() for edge in edges], "path": path,
                "status": "present" if found else "unknown" if truncated or incomplete or self.status(root) != "existing" else "missing" if target else "complete",
                "truncated": sorted(truncated), "coverage_complete": not incomplete}

    def diagnostics(self, kinds: Iterable[str] | None = None) -> dict:
        kinds = None if kinds is None else frozenset(kinds)
        self._build_reverse()
        result = {"dangling": [], "special": [], "type_mismatch": []}
        for node, edges in self._reverse.items():
            status = self.status(node)
            category = "special" if status.startswith("special:") else status
            if category in result:
                result[category].extend({**edge.to_dict(), "target_status": status}
                                        for edge in edges if kinds is None or edge.kind in kinds)
        return result

    def audit(self, expectations: dict, **bounds) -> list[dict]:
        _validate_bounds(**bounds)
        if not isinstance(expectations, dict) or set(expectations) != {"version", "expectations"} or type(expectations["version"]) is not int or expectations["version"] != 1:
            raise ValueError("expectations require version 1 and expectations list")
        requirements = expectations["expectations"]
        if not isinstance(requirements, list):
            raise ValueError("expectations must be a list")
        results = []
        for item in requirements:
            if not isinstance(item, dict) or set(item) != {"id", "source", "target", "kinds", "mode"}:
                raise ValueError("expectation requires id, source, target, kinds and mode")
            if not isinstance(item["id"], str) or not isinstance(item["kinds"], list) or not item["kinds"] or any(not isinstance(k, str) or not _KIND.fullmatch(k) for k in item["kinds"]):
                raise ValueError("invalid expectation id or kinds")
            if item["mode"] not in ("direct", "path"):
                raise ValueError("expectation mode must be direct or path")
            options = dict(bounds)
            if item["mode"] == "direct":
                options["max_depth"] = 1
            source, target = parse_node(item["source"]), parse_node(item["target"])
            if self.status(source) != "existing" or self.status(target) != "existing":
                results.append({"id": item["id"], "status": "unknown", "reason": "endpoint is absent or special", "path": []})
                continue
            if item["mode"] == "direct":
                candidates = self.outgoing(source, item["kinds"])
                observed = candidates[:options.get("max_edges", 1000)]
                match = next((edge for edge in observed if edge.target == target), None)
                bounded = len(observed) < len(candidates)
                status = "present" if match else "unknown" if bounded or not self.complete(source, item["kinds"]) else "missing"
                results.append({"id": item["id"], "status": status, "path": [match.to_dict()] if match else [],
                                "truncated": ["max_edges"] if bounded else [],
                                "coverage_complete": self.complete(source, item["kinds"])})
                continue
            answer = self.traverse(source, target=target, kinds=item["kinds"], **options)
            results.append({"id": item["id"], "status": answer["status"], "path": answer["path"],
                            "truncated": answer["truncated"], "coverage_complete": answer["coverage_complete"]})
        return results

    def _load_overlay(self, overlay: dict, digest: str | None) -> None:
        if not isinstance(overlay, dict) or set(overlay) != {"version", "snapshot_sha256", "source", "edges", "coverage"}:
            raise ValueError("overlay requires version, snapshot_sha256, source, edges and coverage")
        if type(overlay["version"]) is not int or overlay["version"] != 1 or not digest or overlay["snapshot_sha256"] != digest or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("overlay snapshot SHA256 does not match this dump or version is unsupported")
        label = overlay["source"]
        if not isinstance(label, str) or not label or len(label) > 200:
            raise ValueError("overlay source must be a short nonempty label")
        if not isinstance(overlay["edges"], list) or not isinstance(overlay["coverage"], list):
            raise ValueError("overlay edges and coverage must be lists")
        for item in overlay["edges"]:
            if not isinstance(item, dict) or set(item) != {"source", "target", "kind", "evidence"}:
                raise ValueError("overlay edge requires source, target, kind and evidence")
            kind, evidence = item["kind"], item["evidence"]
            if not isinstance(kind, str) or not _KIND.fullmatch(kind) or kind in BUILTIN_KINDS:
                raise ValueError("overlay kind must be a distinct valid relation name")
            if not isinstance(evidence, list) or not evidence or any(not isinstance(s, str) or not s or len(s) > 500 for s in evidence):
                raise ValueError("overlay evidence must be a nonempty list of short strings")
            source, target = parse_node(item["source"]), parse_node(item["target"])
            if self.status(source) != "existing":
                raise ValueError("overlay source node does not exist in this snapshot")
            self._external[source].append(Edge(source, target, kind, tuple(evidence), "external:" + label))
            self.kinds.add(kind)
        for scope in overlay["coverage"]:
            if not isinstance(scope, dict) or set(scope) != {"kind", "sources"}:
                raise ValueError("coverage requires kind and sources")
            kind, sources = scope["kind"], scope["sources"]
            if not isinstance(kind, str) or not _KIND.fullmatch(kind) or kind in BUILTIN_KINDS or kind in self._coverage:
                raise ValueError("invalid or duplicate coverage kind")
            if sources != "*" and not isinstance(sources, list):
                raise ValueError("coverage sources must be * or a list of node identities")
            nodes = None if sources == "*" else {parse_node(s) for s in sources}
            if nodes is not None and any(self.status(node) != "existing" for node in nodes):
                raise ValueError("coverage source node does not exist")
            self._coverage[kind] = nodes
            self.kinds.add(kind)
