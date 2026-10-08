"""Historical inputs are immutable Git blobs, never checked-out working files."""
from pathlib import Path
import os
import subprocess

import pytest

from lambdamoo_db.diff_git import GitRepository
from lambdamoo_db.diff_sources import SourceError
from lambdamoo_db.split import write_split


FIXTURE = Path(__file__).parent / 'fixtures' / 'TypedMapKeys.db'


def git(repo, *args, data=None, env=None):
    result = subprocess.run(['git', '-C', str(repo), *args], input=data, capture_output=True, check=True, env=env)
    return result.stdout.decode().strip()


@pytest.fixture
def history_repo(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    original = FIXTURE.read_bytes()
    commits = []
    for number, data in enumerate((original, original.replace(b'map_test', b'map_best'), original)):
        (tmp_path / 'mongoose.db.new').write_bytes(data)
        git(tmp_path, 'add', 'mongoose.db.new')
        env = dict(os.environ, GIT_AUTHOR_DATE=f'2026-01-0{number + 1}T00:00:00Z', GIT_COMMITTER_DATE=f'2026-01-0{number + 1}T00:00:00Z')
        git(tmp_path, 'commit', '-q', '-m', f'Checkpoint 2026-01-0{number + 1}T00:00:00Z', env=env)
        commits.append(git(tmp_path, 'rev-parse', 'HEAD'))
    return tmp_path, commits, original


def test_endpoint_and_adjacent_edges_preserve_change_and_revert(history_repo):
    path, commits, _ = history_repo
    repo = GitRepository(path)
    assert repo.pairs(commits[0], commits[2], mode='endpoints') == [(commits[0], commits[2])]
    assert repo.pairs(commits[0], commits[2], mode='adjacent') == list(zip(commits, commits[1:]))
    assert repo.snapshot(commits[0]).metadata['sha256'] == repo.snapshot(commits[2]).metadata['sha256']
    assert repo.snapshot(commits[1]).metadata['sha256'] != repo.snapshot(commits[0]).metadata['sha256']
    assert repo.snapshot(commits[0]).metadata['git']['commit'] == commits[0]


def test_date_selection_uses_pinned_tip_and_committer_cutoff(history_repo):
    path, commits, _ = history_repo
    repo = GitRepository(path)
    assert repo.resolve_date(commits[-1], '2026-01-02T12:00:00Z') == commits[1]
    with pytest.raises(SourceError, match='timezone'):
        repo.resolve_date(commits[-1], '2026-01-02')


@pytest.mark.parametrize('cutoff', ['2026-01-02T00:00:00.1Z', '2026-01-02T00:00:00.12345+00:00',
                                  '2026-01-02T00:00:00.123456789Z'])
def test_date_fraction_contract_on_supported_python(history_repo, cutoff):
    path, commits, _ = history_repo
    assert GitRepository(path).resolve_date(commits[-1], cutoff) == commits[1]


def test_edge_cap_is_checked_before_sources_are_loaded(history_repo, monkeypatch):
    path, commits, _ = history_repo
    repo = GitRepository(path)
    monkeypatch.setattr(repo, 'snapshot', lambda *_: pytest.fail('must not load'))
    with pytest.raises(SourceError, match='cap'):
        repo.pairs(commits[0], commits[-1], mode='adjacent', max_edges=1)


def test_split_is_preferred_and_corrupt_split_never_falls_back(history_repo):
    path, _, data = history_repo
    split = path / 'db'
    write_split(data, split)
    git(path, 'add', 'db')
    git(path, 'commit', '-q', '-m', 'split')
    commit = git(path, 'rev-parse', 'HEAD')
    repo = GitRepository(path)
    assert repo.snapshot(commit).metadata['git']['layout'] == 'split'
    (split / 'header.moo').write_bytes(b'corrupt\n')
    git(path, 'add', 'db/header.moo')
    git(path, 'commit', '-q', '-m', 'corrupt split')
    with pytest.raises(SourceError, match='split'):
        repo.snapshot('HEAD')
    assert repo.snapshot('HEAD', source_layout='legacy').db.version == 17


def test_replace_refs_cannot_substitute_a_snapshot(history_repo):
    path, commits, _ = history_repo
    git(path, 'replace', commits[0], commits[1])
    repo = GitRepository(path)
    assert repo.snapshot(commits[0]).metadata['sha256'] != repo.snapshot(commits[1]).metadata['sha256']


def test_hostile_revision_is_data_and_never_a_shell_command(history_repo):
    path, _, _ = history_repo
    repo = GitRepository(path)
    with pytest.raises(SourceError):
        repo.resolve('HEAD; echo credential > injected')
    assert not (path / 'injected').exists()


def test_symlink_git_mode_is_rejected_before_reconstruction(history_repo):
    path, _, data = history_repo
    write_split(data, path / 'db')
    git(path, 'add', 'db')
    target = next((path / 'db' / 'objects').iterdir()).relative_to(path).as_posix()
    blob = git(path, 'hash-object', '-w', '--stdin', data=b'/private/outside')
    git(path, 'update-index', '--cacheinfo', '120000', blob, target)
    git(path, 'commit', '-q', '-m', 'unsafe tree mode')
    with pytest.raises(SourceError, match='regular'):
        GitRepository(path).snapshot('HEAD')


def test_endpoints_require_available_ancestry(history_repo):
    path, commits, _ = history_repo
    git(path,'checkout','--orphan','unrelated','-q')
    git(path,'commit','-q','-m','unrelated world')
    tip = git(path,'rev-parse','HEAD')
    with pytest.raises(SourceError,match='nonancestral'):
        GitRepository(path).pairs(commits[0],tip,mode='endpoints')


def test_explicit_repository_ignores_environment_repository_override(history_repo,monkeypatch):
    path, commits, _ = history_repo
    monkeypatch.setenv('GIT_DIR',str(path/'missing.git'))
    monkeypatch.setenv('GIT_WORK_TREE',str(path/'outside'))
    assert GitRepository(path).resolve('HEAD') == commits[-1]
