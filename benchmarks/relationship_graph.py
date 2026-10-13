"""Reproducible graph measurements; outputs counts/timings, never dump values.

Run from a checkout: uv run python benchmarks/relationship_graph.py
Add --db DUMP --ref '#0' for CLI cold-index and repeated indexed measurements.
Use an immutable local dump. Keep resulting JSON outside the repository.
"""

import argparse
import gc
import json
import platform
from pathlib import Path
import statistics
import struct
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from click.testing import CliRunner

from lambdamoo_db.cli import moodb
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum, Property
from lambdamoo_db.relationship_graph import Node, RelationshipGraph


def summary(samples):
    return {"seconds": samples, "median_seconds": statistics.median(samples),
            "min_seconds": min(samples), "max_seconds": max(samples)}


def synthetic(size, repeats):
    db = MooDatabase()
    for oid in range(size):
        obj = MooObject(oid, "synthetic", 0, 0, -1)
        obj.properties = [Property("links", [ObjNum((oid + 1) % size), ObjNum((oid + 7) % size)], 0, 0)]
        obj.propdefs_count = 1
        db.objects[oid] = obj
    outgoing, reverse, reuse = [], [], []
    for _ in range(repeats):
        gc.collect()
        graph = RelationshipGraph(db)
        start = time.perf_counter()
        graph.outgoing(Node("object", 0), ["property"])
        outgoing.append(time.perf_counter() - start)
        start = time.perf_counter()
        found = graph.incoming(Node("object", 0), ["property"])
        reverse.append(time.perf_counter() - start)
        assert len(found) == 2 and graph.stats["reverse_builds"] == 1
        start = time.perf_counter()
        for oid in range(100):
            graph.incoming(Node("object", oid % size), ["property"])
        reuse.append(time.perf_counter() - start)
        assert graph.stats["reverse_builds"] == 1 and graph.stats["nodes_indexed"] == size
    return {"objects": size, "property_edges": size * 2,
            "outgoing": summary(outgoing), "reverse_first": summary(reverse),
            "100_reused_incoming_queries": summary(reuse)}


def actual(path, ref, repeats):
    with tempfile.TemporaryDirectory(prefix="moodb-graph-benchmark-") as cache:
        prefix = ["--db", str(path), "--cache-dir", cache]
        runner = CliRunner()
        def query(args):
            start = time.perf_counter()
            result = runner.invoke(moodb, prefix + args)
            duration = time.perf_counter() - start
            if result.exit_code != 0:
                raise RuntimeError("benchmark CLI failed; inspect exit/exception locally") from result.exception
            return duration, result.output
        cold, _ = query(["graph", "outgoing", ref, "--limit", "0", "--format", "json"])
        measurements = {}
        digest = None
        for name, args in (
            ("outgoing_indexed", ["graph", "outgoing", ref, "--limit", "0", "--format", "json"]),
            ("incoming_indexed", ["graph", "incoming", ref, "--limit", "0", "--format", "json"]),
            ("two_incoming_batch", ["batch", f"graph incoming {ref} --limit 0 --format json",
                                   f"graph incoming {ref} --limit 0 --format json"]),
            ("fingerprint", ["graph", "fingerprint", "--format", "json"]),
        ):
            samples = []
            count = None
            for _ in range(repeats):
                gc.collect()
                duration, output = query(args)
                samples.append(duration)
                if name in ("outgoing_indexed", "incoming_indexed"):
                    count = json.loads(output)["total_edges"]
                elif name == "fingerprint":
                    digest = json.loads(output)["snapshot_sha256"]
            measurements[name] = {**summary(samples), "reported_edges": count}
        return {"dump_bytes": path.stat().st_size, "snapshot_sha256": digest, "cold_index_and_outgoing_seconds": cold,
                "measurements": measurements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=20000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--ref", default="#0")
    args = parser.parse_args()
    if args.size < 8 or args.repeats < 1:
        parser.error("size must be >=8 and repeats >=1")
    report = {"python": sys.version.split()[0], "platform": platform.platform(), "process_bits": struct.calcsize("P") * 8,
              "synthetic": synthetic(args.size, args.repeats)}
    if args.db:
        report["actual_cli"] = actual(args.db, args.ref, args.repeats)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
