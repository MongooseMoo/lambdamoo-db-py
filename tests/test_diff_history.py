import json
import os
from pathlib import Path
import subprocess

from click.testing import CliRunner
import pytest

from lambdamoo_db.cli import moodb
from lambdamoo_db.diff_history import build_history, read_history


FIXTURE = Path(__file__).parent / 'fixtures' / 'TypedMapKeys.db'


def git(path, *args):
    return subprocess.run(['git','-C',str(path),*args], capture_output=True, check=True).stdout.decode().strip()


@pytest.fixture
def timeline(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo,'init','-q')
    git(repo,'config','user.name','Test')
    git(repo,'config','user.email','test@example.invalid')
    original = FIXTURE.read_bytes()
    commits = []
    for data in (original, original.replace(b'map_test',b'map_best'), original):
        (repo/'mongoose.db.new').write_bytes(data)
        git(repo,'add','mongoose.db.new')
        git(repo,'commit','-q','-m','checkpoint')
        commits.append(git(repo,'rev-parse','HEAD'))
    return repo, commits


def test_adjacent_bundle_retains_revert_and_supports_indexes(timeline,tmp_path):
    repo, commits = timeline
    manifest = build_history(repo,commits[0],commits[-1],tmp_path/'history',mode='adjacent')
    assert manifest['status'] == 'different'
    edges = read_history(tmp_path/'history')['edges']
    assert len(edges) == 2
    assert edges[0]['edge_id'] != edges[1]['edge_id']
    page = read_history(tmp_path/'history',edge=edges[0]['edge_id'],object_id='#0')
    assert page
    with pytest.raises(ValueError,match='edge'):
        read_history(tmp_path/'history',event='unknown')


def test_endpoints_equal_but_adjacent_different_and_existing_destination_safe(timeline,tmp_path):
    repo, commits = timeline
    output = tmp_path/'endpoints'
    assert build_history(repo,commits[0],commits[-1],output)['status'] == 'equal'
    before = (output/'manifest.json').read_bytes()
    with pytest.raises(ValueError,match='exist'):
        build_history(repo,commits[0],commits[-1],output)
    assert (output/'manifest.json').read_bytes() == before


def test_resume_validates_completed_edge_evidence(timeline,tmp_path,monkeypatch):
    repo, commits = timeline
    first, second = tmp_path/'first',tmp_path/'second'
    build_history(repo,commits[0],commits[-1],first,mode='adjacent')
    import lambdamoo_db.diff_history as history
    monkeypatch.setattr(history,'compare_snapshots',lambda *_: pytest.fail('validated edges should be reused'))
    manifest = build_history(repo,commits[0],commits[-1],second,mode='adjacent',resume_from=first)
    assert manifest['reused_edges'] == 2
    edges = read_history(first)['edges']
    (first/'edges'/edges[0]['edge_id']/'events.jsonl').write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        read_history(first)


def test_keep_going_does_not_bridge_missing_checkpoint(timeline,tmp_path):
    repo, commits = timeline
    (repo/'mongoose.db.new').write_bytes(b'invalid dump\n')
    git(repo,'add','mongoose.db.new')
    git(repo,'commit','-q','-m','corrupt')
    bad = git(repo,'rev-parse','HEAD')
    manifest = build_history(repo,commits[0],bad,tmp_path/'failed',mode='adjacent',keep_going=True)
    assert manifest['status'] == 'error'
    assert not manifest['scan_complete']
    assert read_history(tmp_path/'failed')['edges'][-1]['status'] == 'error'


@pytest.mark.parametrize('interruption', [ValueError, KeyboardInterrupt])
def test_failed_history_publishes_verified_prefix_for_resume(timeline, tmp_path, monkeypatch, interruption):
    from lambdamoo_db.diff_git import GitRepository
    repo, commits = timeline
    original = GitRepository.snapshot
    def interrupted(self, ref, **kwargs):
        if ref == commits[-1]:
            raise interruption('synthetic interrupted acquisition')
        return original(self, ref, **kwargs)
    first = tmp_path / 'interrupted'
    monkeypatch.setattr(GitRepository, 'snapshot', interrupted)
    result = build_history(repo, commits[0], commits[-1], first, mode='adjacent')
    assert result['status'] == 'error' and not result['scan_complete']
    assert result['new_commit'] == commits[1]
    assert result['requested_new_commit'] == commits[-1]
    assert len(read_history(first)['edges']) == 1
    before = (first / 'manifest.json').read_bytes()
    monkeypatch.setattr(GitRepository, 'snapshot', original)
    resumed = build_history(repo, commits[0], commits[-1], tmp_path / 'resumed',
                            mode='adjacent', resume_from=first)
    assert resumed['reused_edges'] == 1 and resumed['scan_complete']
    assert (first / 'manifest.json').read_bytes() == before


def test_history_cli_ignores_dump_environment(timeline,tmp_path):
    repo, commits = timeline
    result = CliRunner().invoke(moodb,['history','--repo',str(repo),'--from',commits[0],'--to',commits[-1],
                                      '--output-dir',str(tmp_path/'cli')],env={'MOODB':'absent.db'})
    assert result.exit_code == 0,result.output


def test_history_completeness_tracks_limited_pair(timeline,tmp_path):
    repo, commits = timeline
    manifest = build_history(repo,commits[0],commits[1],tmp_path/'limited',max_events=0)
    assert not manifest['details_complete']
    assert manifest['payloads_complete']


def test_history_edges_are_bound_to_pair_provenance(timeline,tmp_path):
    import hashlib
    repo, commits = timeline
    root = tmp_path/'history'
    build_history(repo,commits[0],commits[1],root)
    edges = [json.loads(line) for line in (root/'edges.jsonl').read_bytes().splitlines()]
    edges[0]['old_commit'] = commits[2]
    data = b''.join(json.dumps(row).encode()+b'\n' for row in edges)
    (root/'edges.jsonl').write_bytes(data)
    manifest = json.loads((root/'manifest.json').read_bytes())
    manifest['files']['edges.jsonl'] = {'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    (root/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        read_history(root)


@pytest.fixture
def fake_repository(monkeypatch):
    from lambdamoo_db.diff_sources import snapshot_from_bytes
    original = FIXTURE.read_bytes()
    class Repository:
        def __init__(self, _): pass
        def resolve(self, ref): return ref
        def pairs(self, start, end, **_): return [] if start == end else [(start,end)]
        def inventory(self, tip): return {'first_parent_tip':tip,'shallow':False,'branch_tips':[]}
        def snapshot(self, ref, **_):
            data = original if ref == 'a'*40 else original.replace(b'map_test',b'map_best')
            return snapshot_from_bytes(data,kind='git',label=ref,provenance={'commit':ref,'layout':'legacy'})
        def commit_metadata(self, ref): return {'commit':ref,'parents':[],'checkpoint_time':None}
    monkeypatch.setattr('lambdamoo_db.diff_history.GitRepository',Repository)


def test_history_status_is_verified_against_edges(fake_repository,tmp_path):
    root = tmp_path/'history'
    build_history('unused','a'*40,'b'*40,root,kinds=('changed','added'))
    manifest = json.loads((root/'manifest.json').read_bytes())
    assert manifest['status'] == 'different'
    manifest['status'] = 'equal'
    (root/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='status'):
        read_history(root)


def test_strict_redaction_zero_edges_is_error(fake_repository,tmp_path):
    from lambdamoo_db.diff_redaction import RedactionPolicy
    policy = RedactionPolicy.from_data({'schema_version':1,'rules':[{'selector':{'section':'properties'},'action':'conceal'}]})
    manifest = build_history('unused','a'*40,'a'*40,tmp_path/'empty',mode='adjacent',redaction=policy,redact_strict=True)
    assert manifest['status'] == 'error'
    assert manifest['redaction_matches'] == [0]
    assert not manifest['scan_complete']


def test_manifest_link_is_rejected(fake_repository,tmp_path):
    import shutil
    root = tmp_path/'history'
    build_history('unused','a'*40,'b'*40,root)
    outside = tmp_path/'outside.json'
    shutil.copyfile(root/'manifest.json',outside)
    (root/'manifest.json').unlink()
    try:
        (root/'manifest.json').symlink_to(outside)
    except OSError:
        pytest.skip('Host does not permit file symlinks')
    with pytest.raises(ValueError):
        read_history(root)


def test_post_publication_edge_failure_keeps_no_orphan_bundle(fake_repository, tmp_path, monkeypatch):
    from lambdamoo_db.diff_history import GitRepository
    monkeypatch.setattr(GitRepository, 'commit_metadata', lambda *args: (_ for _ in ()).throw(ValueError('synthetic metadata failure')))
    root = tmp_path / 'failed'
    result = build_history('unused', 'a'*40, 'b'*40, root, keep_going=True)
    assert result['status'] == 'error'
    assert list((root / 'edges').iterdir()) == []
    assert not any(name.startswith('edges/') for name in result['files'])
    assert read_history(root)['edges'][0].get('bundle') is None


def test_posix_history_publication_preserves_pair_manifests(fake_repository, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import lambdamoo_db.diff_report as report
    monkeypatch.setattr(report, 'os', SimpleNamespace(name='posix', link=os.link, replace=os.replace))
    root = tmp_path / 'published'
    result = build_history('unused', 'a'*40, 'b'*40, root)
    assert result['status'] == 'different'
    edge = read_history(root)['edges'][0]
    assert (root / edge['bundle'] / 'manifest.json').is_file()
    assert read_history(root, edge=edge['edge_id'], object_id='#0')
