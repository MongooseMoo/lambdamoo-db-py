"""Click commands for evidence-bearing snapshot relationships."""

import hashlib
import json
from pathlib import Path

import click

from .lazy import open_indexed
from .diff_report import _terminal
from .reader import load
from .relationship_graph import BUILTIN_KINDS, Node, RelationshipGraph, parse_node


def register_graph(moodb):
    @moodb.group()
    @click.option("--overlay", type=click.Path(exists=True, dir_okay=False), help="SHA256-bound external relationship evidence JSON.")
    @click.option("--effective", is_flag=True, help="Follow clear slots; include original value holder provenance.")
    @click.option("--labels", is_flag=True, help="Opt in to object and defining property names; never property values or map keys.")
    @click.pass_context
    def graph(ctx, overlay, effective, labels):
        """Inspect typed object/anonymous/waif relationships without values.

        Graph evidence records slots and list/map ordinals, never stored
        names, map-key strings, source code or arbitrary property values.
        External evidence is supplied by the caller and printed verbatim.
        """
        ctx.meta["graph_options"] = (overlay, effective)
        ctx.meta["graph_labels"] = labels

    def database(ctx):
        if "db" not in ctx.obj:
            if ctx.obj["path"] is None:
                raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
            path, cache = ctx.obj["path"], ctx.obj["cache"]
            ctx.obj["db"] = load(path) if cache is None else open_indexed(path, cache)
        return ctx.obj["db"]

    def fingerprint(ctx):
        if ctx.obj["path"] is None:
            raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
        if "graph_sha256" not in ctx.obj:
            digest = hashlib.sha256()
            with open(ctx.obj["path"], "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            ctx.obj["graph_sha256"] = digest.hexdigest()
        return ctx.obj["graph_sha256"]

    def read_json(path):
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError("non-JSON numeric constant")
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_fields,
                              parse_constant=invalid_constant)
        except (OSError, UnicodeError, ValueError):
            raise click.ClickException("cannot read valid UTF-8 JSON input") from None

    def index(ctx):
        overlay, effective = ctx.meta["graph_options"]
        key = (overlay, effective)
        indexes = ctx.obj.setdefault("relationship_graphs", {})
        if key not in indexes:
            payload = read_json(overlay) if overlay else None
            try:
                indexes[key] = RelationshipGraph(database(ctx), effective=effective, overlay=payload,
                                                 snapshot_sha256=fingerprint(ctx) if overlay else None)
            except ValueError as error:
                raise click.ClickException(str(error)) from None
        return indexes[key]

    def node(ctx, value):
        try:
            result = parse_node(value)
        except ValueError:
            from .inspection import LookupFailed, resolve_object
            try:
                obj = resolve_object(database(ctx), value)
                return index(ctx).object_node(obj.id)
            except LookupFailed as error:
                raise click.ClickException(str(error)) from None
        if result.type == "object" and index(ctx).status(result) == "type_mismatch":
            return Node("anonymous", result.id)
        return result

    def selected(ctx, kinds):
        if kinds:
            return tuple(kinds)
        # Property paths answer wiring questions more usefully than shared
        # wizard ownership; structural topology is explicitly selectable.
        return ("property", "waif-property") + tuple(sorted(index(ctx).kinds - BUILTIN_KINDS))

    def render(result, format):
        ctx = click.get_current_context()
        if ctx.meta.get("graph_labels") and "snapshot_sha256" not in result:
            graph_index = index(ctx)
            def annotate(value):
                if isinstance(value, list):
                    return [annotate(item) for item in value]
                if isinstance(value, dict):
                    if {"source", "target", "kind", "evidence", "provenance"}.issubset(value):
                        return {**value, "labels": graph_index.labels(value)}
                    return {key: annotate(item) for key, item in value.items()}
                return value
            result = annotate(result)
        if format == "json":
            click.echo(json.dumps(result, sort_keys=True))
            return
        def echo(value):
            click.echo(_terminal(value))
        def identity(value):
            return str(Node(value["type"], value["id"]))
        if "expectations" in result:
            for requirement in result["expectations"]:
                echo(f"{requirement['id']}: {requirement['status']}")
        elif "snapshot_sha256" in result:
            echo(result["snapshot_sha256"])
        elif "dangling" in result:
            for category in ("dangling", "special", "type_mismatch"):
                echo(f"{category}: {len(result[category])} references")
                for edge in result[category]:
                    echo(f"  {identity(edge['source'])} --{edge['kind']}--> {identity(edge['target'])} [{'/'.join(edge['evidence'])}]")
        else:
            if "status" in result:
                echo(f"{result['status']}; {len(result.get('nodes', []))} nodes; {len(result['edges'])} edges")
            for edge in result.get("path", result.get("edges", [])) if result.get("target") else result.get("edges", []):
                holder = f"; value from {identity(edge['value_from'])}" if edge.get("value_from") else ""
                echo(f"{identity(edge['source'])} --{edge['kind']}--> {identity(edge['target'])} [{'/'.join(edge['evidence'])}; {edge['provenance']}{holder}]")
                if edge.get("labels"):
                    echo("  labels: " + json.dumps(edge["labels"], ensure_ascii=True, sort_keys=True))
            if result.get("truncated"):
                echo("truncated: " + ", ".join(result["truncated"]))
            if result.get("coverage_complete") is False:
                echo("relationship coverage is incomplete")

    def common(function):
        function = click.option("--format", "format", type=click.Choice(["text", "json"]), default="text")(function)
        return click.option("--kind", "kinds", multiple=True, help="Relation to include (repeatable); default property, waif-property and external kinds.")(function)

    def bounds(function):
        for name, default, minimum in (("max-depth", 3, 0), ("max-nodes", 100, 1), ("max-edges", 1000, 0)):
            function = click.option("--" + name, type=click.IntRange(min=minimum), default=default, show_default=True)(function)
        return function

    @graph.command("fingerprint")
    @click.option("--format", type=click.Choice(["text", "json"]), default="text")
    @click.pass_context
    def graph_fingerprint(ctx, format):
        """SHA256 of this dump's bytes, for binding external evidence."""
        render({"snapshot_sha256": fingerprint(ctx)}, format)

    def edge_command(direction):
        @graph.command(direction)
        @click.argument("ref")
        @click.option("--limit", type=click.IntRange(min=0), default=500, show_default=True)
        @common
        @click.pass_context
        def command(ctx, ref, limit, kinds, format):
            """References with exact ordinal evidence and source provenance."""
            graph_index, root = index(ctx), node(ctx, ref)
            chosen = selected(ctx, kinds)
            all_edges = getattr(graph_index, direction)(root, chosen)
            result = {"node": {"type": root.type, "id": root.id}, "node_status": graph_index.status(root),
                      "edges": [edge.to_dict() for edge in all_edges[:limit]], "total_edges": len(all_edges),
                      "truncated": ["limit"] if len(all_edges) > limit else [],
                      "coverage_complete": graph_index.complete(root, chosen) if direction == "outgoing" else
                      graph_index.incoming_complete(chosen)}
            render(result, format)
        return command

    edge_command("outgoing")
    edge_command("incoming")

    @graph.command("neighborhood")
    @click.argument("ref")
    @click.option("--direction", type=click.Choice(["out", "in", "both"]), default="out")
    @bounds
    @common
    @click.pass_context
    def neighborhood(ctx, ref, direction, max_depth, max_nodes, max_edges, kinds, format):
        """Bounded breadth-first neighborhood; reports truncation and coverage."""
        render(index(ctx).traverse(node(ctx, ref), direction=direction, kinds=selected(ctx, kinds),
                                  max_depth=max_depth, max_nodes=max_nodes, max_edges=max_edges), format)

    @graph.command("path")
    @click.argument("source")
    @click.argument("target")
    @bounds
    @common
    @click.pass_context
    def path(ctx, source, target, max_depth, max_nodes, max_edges, kinds, format):
        """Shortest directed path under the selected kinds and bounds.

        present=observed path, missing=complete explored topology, unknown=
        bounds or incomplete coverage. Connectivity does not prove dispatch.
        """
        answer = index(ctx).traverse(node(ctx, source), target=node(ctx, target), kinds=selected(ctx, kinds),
                                     max_depth=max_depth, max_nodes=max_nodes, max_edges=max_edges)
        render(answer, format)

    @graph.command("dangling")
    @common
    @click.pass_context
    def dangling(ctx, kinds, format):
        """Whole-snapshot missing, special-negative and mismatched references."""
        render(index(ctx).diagnostics(selected(ctx, kinds)), format)

    @graph.command("audit")
    @click.argument("expectations", type=click.Path(exists=True, dir_okay=False))
    @bounds
    @click.option("--format", type=click.Choice(["text", "json"]), default="text")
    @click.pass_context
    def audit(ctx, expectations, max_depth, max_nodes, max_edges, format):
        """Check declarative direct/path expectations. Exit 0/1/3: present/missing/unknown."""
        try:
            results = index(ctx).audit(read_json(expectations), max_depth=max_depth,
                                       max_nodes=max_nodes, max_edges=max_edges)
        except ValueError as error:
            raise click.ClickException(str(error)) from None
        render({"expectations": results}, format)
        statuses = {result["status"] for result in results}
        ctx.exit(1 if "missing" in statuses else 3 if "unknown" in statuses else 0)
