"""Synthetic authenticated history query comparison; artifacts stay outside Git.

Run from the checkout: uv run python benchmarks/history_read.py OUTPUT_DIR
    [--rounds 10 --edges 8 --width 40]

OUTPUT_DIR must be new and outside this checkout. Timings compare equivalent
event IDs using repeated old history retrieval versus one temporal query.
"""
import argparse
import gc
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument('output')
parser.add_argument('--rounds', type=int, default=10)
parser.add_argument('--edges', type=int, default=8)
parser.add_argument('--width', type=int, default=40)
args = parser.parse_args()
if min(args.rounds, args.edges, args.width) < 1:
    parser.error('rounds, edges and width must be positive')
workspace = Path(__file__).resolve().parents[1]
output = Path(args.output).resolve()
if output.is_relative_to(workspace):
    parser.error('output must be outside this checkout')
if output.exists():
    parser.error('output directory already exists; choose a new directory')
sys.path.insert(0, str(workspace))
from lambdamoo_db.database import MooDatabase, MooObject, ObjNum, Property
from lambdamoo_db.diff_history import build_history, read_history
from lambdamoo_db.history_read import query_history
import lambdamoo_db.diff_report as report

output.mkdir(parents=True)
root = output/'history'
snapshots = []
commits = [f'{i+1:040x}' for i in range(args.edges+1)]
for i in range(args.edges+1):
    obj = MooObject(1, 'holder', 0, ObjNum(1), ObjNum(-1))
    obj.properties = [Property('wiring', {f'entry-{j}': i % 2 for j in range(args.width)}, ObjNum(1), 0)]
    obj.propdefs_count = 1
    db = MooDatabase(objects={1: obj})
    db.total_objects = 1
    db.version = 17
    db.versionstring = 'synthetic'
    snapshots.append(db)

class Repository:
    def __init__(self, _): pass
    def resolve(self, ref): return ref
    def pairs(self, *_, **kwargs): return list(zip(commits, commits[1:]))
    def inventory(self, tip): return {'first_parent_tip': tip, 'shallow': False, 'branch_tips': []}
    def snapshot(self, ref, **kwargs):
        i = commits.index(ref)
        return SimpleNamespace(db=snapshots[i], metadata={'kind': 'synthetic',
            'sha256': hashlib.sha256(str(i).encode()).hexdigest(), 'bytes': i+1, 'format': 17})
    def commit_metadata(self, ref):
        i = commits.index(ref)
        return {'commit': ref, 'parents': commits[i-1:i] if i else [], 'checkpoint_time': None}

with patch('lambdamoo_db.diff_history.GitRepository', Repository):
    build_history('synthetic', commits[0], commits[-1], root, mode='adjacent')

def previous():
    result = []
    for edge in read_history(root, limit=args.edges+1)['edges']:
        result.extend(read_history(root, edge=edge['edge_id'], object_id=1, limit=args.width+1))
    return result

def current():
    result = query_history(root, object_id=1, limit=args.edges*args.width+1,
                           max_scan_events=args.edges*args.width+1, max_scan_edges=args.edges+1)
    return [row['event'] for row in result['events']]

assert [e['event_id'] for e in previous()] == [e['event_id'] for e in current()]
times = {'previous': [], 'current': []}
for i in range(args.rounds):
    for name, function in [('previous', previous), ('current', current)] if i % 2 == 0 else [('current', current), ('previous', previous)]:
        gc.collect()
        started = time.perf_counter()
        function()
        times[name].append(time.perf_counter()-started)

measurements = {}
original = report._load_bundle
for name, function in [('previous', previous), ('current', current)]:
    calls = []
    def counted(path):
        calls.append(str(path))
        return original(path)
    with patch.object(report, '_load_bundle', counted):
        function()
    gc.collect()
    tracemalloc.start()
    function()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    measurements[name] = {'seconds': times[name], 'median_seconds': statistics.median(times[name]),
                          'pair_validations': len(calls), 'peak_traced_bytes': peak}

result = {'fixture': 'synthetic parsed models; repeated integer map-value change and revert; authenticated history bundles',
          'python': sys.version, 'platform': platform.platform(), 'rounds': args.rounds,
          'edges': args.edges, 'events_per_edge': args.width, 'returned_events': len(current()),
          'total_artifact_bytes': sum(p.stat().st_size for p in root.rglob('*') if p.is_file()),
          'measurements': measurements, 'notes': 'Warm filesystem; alternating execution order; timings exclude tracemalloc; memory uses tracemalloc separate run, not RSS; full authentication retained.'}
(output/'results.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result, indent=2))
