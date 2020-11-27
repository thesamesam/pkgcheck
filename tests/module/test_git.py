import os
import subprocess
from unittest.mock import Mock, patch

import pytest
from pkgcore.ebuild import atom
from pkgcore.restrictions import packages
from pkgcheck import base, git
from snakeoil.cli.exceptions import UserException
from snakeoil.fileutils import touch
from snakeoil.osutils import pjoin
from snakeoil.process import CommandNotFound, find_binary


# skip testing module if git isn't installed
try:
    find_binary('git')
except CommandNotFound:
    pytestmark = pytest.mark.skipif(True, reason='git not installed')


class TestPkgcheckScanCommitsParseArgs:

    @pytest.fixture(autouse=True)
    def _setup(self, tool):
        self.tool = tool
        self.args = ['scan']

    def test_commits_with_targets(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            options, _func = self.tool.parse_args(self.args + ['--commits', 'ref', 'dev-util/foo'])
        assert excinfo.value.code == 2
        out, err = capsys.readouterr()
        err = err.strip().split('\n')
        assert err[-1].startswith(
            "pkgcheck scan: error: --commits is mutually exclusive with target: dev-util/foo")

    def test_commits_git_unavailable(self, capsys):
        with patch('subprocess.run') as git_diff:
            git_diff.side_effect = FileNotFoundError
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + ['--commits'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith(
                "pkgcheck scan: error: git not available to determine targets for --commits")

    def test_git_error(self, capsys):
        with patch('subprocess.run') as git_diff:
            git_diff.side_effect = subprocess.CalledProcessError(1, 'git')
            git_diff.side_effect.stderr = 'git error: foobar'
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + ['--commits'])
            assert excinfo.value.code == 2
            out, err = capsys.readouterr()
            err = err.strip().split('\n')
            assert err[-1].startswith('pkgcheck scan: error: failed running git: ')

    def test_commits_nonexistent(self):
        with patch('subprocess.run') as git_diff:
            git_diff.return_value.stdout = ''
            with pytest.raises(SystemExit) as excinfo:
                options, _func = self.tool.parse_args(self.args + ['--commits'])
            assert excinfo.value.code == 0

    def test_commits_existing(self):
        output = [
            'dev-libs/foo/metadata.xml\n',
            'media-libs/bar/bar-0.ebuild\n',
        ]
        with patch('subprocess.run') as git_diff:
            git_diff.return_value.stdout = ''.join(output)
            options, _func = self.tool.parse_args(self.args + ['--commits'])
            atom_restricts = [atom.atom('dev-libs/foo'), atom.atom('media-libs/bar')]
            assert list(options.restrictions) == \
                [(base.package_scope, packages.OrRestriction(*atom_restricts))]

    def test_commits_eclasses(self):
        output = [
            'dev-libs/foo/metadata.xml\n',
            'media-libs/bar/bar-0.ebuild\n',
            'eclass/foo.eclass\n',
        ]
        with patch('subprocess.run') as git_diff:
            git_diff.return_value.stdout = ''.join(output)
            options, _func = self.tool.parse_args(self.args + ['--commits'])
            atom_restricts = [atom.atom('dev-libs/foo'), atom.atom('media-libs/bar')]
            restrictions = list(options.restrictions)
            assert len(restrictions) == 2
            assert restrictions[0] == \
                (base.package_scope, packages.OrRestriction(*atom_restricts))
            assert restrictions[1][0] == base.eclass_scope
            assert restrictions[1][1].match(['foo'])

    def test_commits_ignored_changes(self):
        output = [
            'foo/bar.txt\n',
            'eclass/tests/check.sh\n',
        ]
        with patch('subprocess.run') as git_diff:
            git_diff.return_value.stdout = ''.join(output)
            with pytest.raises(SystemExit) as excinfo:
                self.tool.parse_args(self.args + ['--commits'])
            assert excinfo.value.code == 0


class TestGitStash:

    def test_non_git_repo(self, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            with git.GitStash(tmp_path):
                pass
        assert 'not a git repo' in str(excinfo.value)

    def test_empty_git_repo(self, git_repo):
        with git.GitStash(git_repo.path):
            pass

    def test_untracked_file(self, git_repo):
        path = pjoin(git_repo.path, 'foo')
        touch(path)
        assert os.path.exists(path)
        with git.GitStash(git_repo.path):
            assert not os.path.exists(path)
        assert os.path.exists(path)

    def test_failed_stashing(self, git_repo):
        path = pjoin(git_repo.path, 'foo')
        touch(path)
        assert os.path.exists(path)
        with patch('subprocess.run') as run:
            err = subprocess.CalledProcessError(1, 'git stash')
            err.stderr = 'git stash failed'
            run.side_effect = [Mock(stdout='foo'), err]
            with pytest.raises(UserException) as excinfo:
                with git.GitStash(git_repo.path):
                    pass
            assert 'git failed stashing files' in str(excinfo.value)

    def test_failed_unstashing(self, git_repo):
        path = pjoin(git_repo.path, 'foo')
        touch(path)
        assert os.path.exists(path)
        with pytest.raises(UserException) as excinfo:
            with git.GitStash(git_repo.path):
                assert not os.path.exists(path)
                touch(path)
        assert 'git failed applying stash' in str(excinfo.value)


class TestParsedGitRepo:

    def test_non_git(self, tmp_path):
        with pytest.raises(git.GitError):
            git.ParsedGitRepo(str(tmp_path))

    def test_empty_repo(self, make_git_repo):
        git_repo = make_git_repo()
        p = git.ParsedGitRepo(git_repo.path)
        with pytest.raises(git.GitError) as excinfo:
            list(p.parse_git_log('HEAD'))
        assert 'failed running git log' in str(excinfo)

    def test_commits_parsing(self, make_git_repo):
        git_repo = make_git_repo()

        # make an initial commit
        git_repo.add('foo', msg='foo', create=True)
        p = git.ParsedGitRepo(git_repo.path)
        commits = list(p.parse_git_log('HEAD'))
        assert len(commits) == 1
        orig_commit = commits[0]
        assert orig_commit.message == ['foo']

        # make another commit
        git_repo.add('bar', msg='bar', create=True)
        commits = list(p.parse_git_log('HEAD'))
        assert len(commits) == 2
        assert commits[0].message == ['bar']
        assert commits[1] == orig_commit
        assert len(set(commits)) == 2

    def test_pkgs_parsing(self, repo, make_git_repo):
        git_repo = make_git_repo(repo.location, commit=True)
        p = git.ParsedGitRepo(git_repo.path)

        # initialize the dict cache
        data = {}
        p.update('HEAD', data=data)
        assert data == {}

        # create a pkg and commit it
        repo.create_ebuild('cat/pkg-0')
        git_repo.add_all('cat/pkg-0')
        pkgs = list(p.parse_git_log('HEAD', pkgs=True))
        assert len(pkgs) == 1
        pkg = pkgs[0]
        assert pkg.atom == atom.atom('=cat/pkg-0')
        assert pkg.status == 'A'
        assert pkg.commit.message == ['cat/pkg-0']

        # update the dict cache
        p.update('HEAD', data=data)
        assert len(data['cat']['pkg']['A']) == 1
        commit = git_repo.HEAD

        # add a new version and commit it
        repo.create_ebuild('cat/pkg-1')
        git_repo.add_all('cat/pkg-1')
        pkgs = list(p.parse_git_log('HEAD', pkgs=True))
        assert len(pkgs) == 2
        pkg = pkgs[0]
        assert pkg.atom == atom.atom('=cat/pkg-1')
        assert pkg.status == 'A'

        # update the dict cache
        p.update(f'{commit}..HEAD', data=data)
        assert len(data['cat']['pkg']['A']) == 2
        commit = git_repo.HEAD

        # remove the old version
        git_repo.remove('cat/pkg/pkg-0.ebuild')
        pkgs = list(p.parse_git_log('HEAD', pkgs=True))
        assert len(pkgs) == 3
        pkg = pkgs[0]
        assert pkg.atom == atom.atom('=cat/pkg-0')
        assert pkg.status == 'D'

        # update the dict cache
        p.update(f'{commit}..HEAD', data=data)
        assert len(data['cat']['pkg']['A']) == 2
        assert len(data['cat']['pkg']['D']) == 1
        commit = git_repo.HEAD

        # rename the pkg
        git_repo.move('cat', 'cat2')
        pkgs = list(p.parse_git_log('HEAD', pkgs=True))
        assert len(pkgs) == 4
        pkg = pkgs[0]
        assert pkg.atom == atom.atom('=cat/pkg-1')
        assert pkg.status == 'R'

        # update the dict cache
        p.update(f'{commit}..HEAD', data=data)
        assert len(data['cat']['pkg']['A']) == 2
        assert len(data['cat']['pkg']['D']) == 1
        assert len(data['cat']['pkg']['R']) == 1
        commit = git_repo.HEAD

        # overlay repo objects on top of the dict cache
        changed_repo = git.GitChangedRepo(data)
        assert len(changed_repo) == 4
        modified_repo = git.GitModifiedRepo(data)
        assert len(modified_repo) == 3
        added_repo = git.GitAddedRepo(data)
        assert len(added_repo) == 3
        removed_repo = git.GitRemovedRepo(data)
        assert len(removed_repo) == 1


class TestGitAddon:

    @pytest.fixture(autouse=True)
    def _setup(self, tool, tmp_path, repo):
        self.repo = repo
        self.cache_dir = str(tmp_path)

        args = ['scan', '--cache-dir', self.cache_dir, '--repo', self.repo.location]
        options, _ = tool.parse_args(args)
        self.addon = git.GitAddon(options)
        self.cache_file = self.addon.cache_file(self.repo)

    def test_git_unavailable(self, tool):
        args = ['scan', '--cache-dir', self.cache_dir, '--repo', self.repo.location]
        options, _ = tool.parse_args(args)
        assert options.cache['git']
        with patch('pkgcheck.git.find_binary') as find_binary:
            find_binary.side_effect = CommandNotFound('git not found')
            addon = git.GitAddon(options)
            assert not addon.options.cache['git']

    def test_no_gitignore(self):
        assert self.addon._gitignore is None
        assert not self.addon.gitignored('')

    def test_failed_gitignore(self):
        with open(pjoin(self.repo.location, '.gitignore'), 'w') as f:
            f.write('.*.swp\n')
        with patch('pkgcheck.git.open') as fake_open:
            fake_open.side_effect = IOError('file reading failure')
            assert self.addon._gitignore is None

    def test_gitignore(self):
        for path in ('.gitignore', '.git/info/exclude'):
            file_path = pjoin(self.repo.location, path)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w') as f:
                f.write('.*.swp\n')
            assert self.addon.gitignored('.foo.swp')
            assert self.addon.gitignored(pjoin(self.repo.location, '.foo.swp'))
            assert not self.addon.gitignored('foo.swp')
            assert not self.addon.gitignored(pjoin(self.repo.location, 'foo.swp'))
