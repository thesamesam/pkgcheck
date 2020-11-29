from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from pkgcheck.checks import git as git_mod
from pkgcheck.git import GitCommit
from pkgcore.ebuild.cpv import VersionedCPV as CPV
from pkgcore.test.misc import FakeRepo
from snakeoil.cli import arghparse
from snakeoil.fileutils import touch
from snakeoil.osutils import pjoin

from ..misc import ReportTestCase, init_check


class FakeCommit(GitCommit):
    """Fake git commit objects with default values."""

    def __init__(self, **kwargs):
        commit_data = {
            'hash': '7f9abd7ec2d079b1d0c36fc2f5d626ae0691757e',
            'commit_date': 'Sun Dec 8 02:13:58 2019 -0700',
            'author': 'author@domain.com',
            'committer': 'committer@domain.com',
            'message': (),
        }
        commit_data.update(kwargs)
        super().__init__(**commit_data)


class TestGitCheck(ReportTestCase):
    check_kls = git_mod.GitCommitsCheck
    options = arghparse.Namespace(
        target_repo=FakeRepo(), commits='origin', gentoo_repo=True)
    check = git_mod.GitCommitsCheck(options)

    def test_sign_offs(self):
        # assert that it checks for both author and comitter
        r = self.assertReport(
            self.check,
            FakeCommit(author='user1', committer='user2', message=['blah'])
        )
        assert isinstance(r, git_mod.MissingSignOff)
        assert r.missing_sign_offs == ('user1', 'user2')

        # assert that it handles author/committer being the same
        self.assertNoReport(
            self.check,
            FakeCommit(
                author='user@user.com', committer='user@user.com',
                message=['summary', '', 'Signed-off-by: user@user.com']))

        # assert it can handle multiple sign offs.
        self.assertNoReport(
            self.check,
            FakeCommit(
                author='user1', committer='user2',
                message=['summary', '', 'Signed-off-by: user2', 'Signed-off-by: user1']))

    def SO_commit(self, summary='summary', body='', tags=(), **kwargs):
        """Create a commit object from summary, body, and tags components."""
        author = kwargs.pop('author', 'author@domain.com')
        committer = kwargs.pop('committer', 'author@domain.com')
        message = summary
        if message:
            if body:
                message += '\n\n' + body
            sign_offs = tuple(f'Signed-off-by: {user}' for user in {author, committer})
            message += '\n\n' + '\n'.join(tuple(tags) + sign_offs)
        return FakeCommit(author=author, committer=committer, message=message.splitlines())

    def test_invalid_commit_tag(self):
        # assert it doesn't puke if there are no tags
        self.assertNoReport(self.check, self.SO_commit())

        self.assertNoReport(self.check, self.SO_commit(tags=['Bug: https://gentoo.org/blah']))
        self.assertNoReport(self.check, self.SO_commit(tags=['Close: https://gentoo.org/blah']))

        r = self.assertReport(self.check, self.SO_commit(tags=['Bug: 123455']))
        assert isinstance(r, git_mod.InvalidCommitTag)
        assert (r.tag, r.value, r.error) == ('Bug', '123455', "value isn't a URL")

        # Do a protocol check; this is more of an assertion against the parsing model
        # used in the implementation.
        r = self.assertReport(self.check, self.SO_commit(tags=['Closes: ftp://blah.com/asdf']))
        assert isinstance(r, git_mod.InvalidCommitTag)
        assert r.tag == 'Closes'
        assert 'protocol' in r.error

    def test_gentoo_bug_tag(self):
        commit = self.SO_commit(tags=['Gentoo-Bug: https://bugs.gentoo.org/1'])
        assert 'Gentoo-Bug tag is no longer valid' in self.assertReport(self.check, commit).error

    def test_commit_tags(self):
        ref = 'd8337304f09'

        for tag in ('Fixes', 'Reverts'):
            # no results on `git cat-file` failure
            with patch('subprocess.Popen') as git_cat:
                # force using a new `git cat-file` process for each iteration
                self.check._git_cat_file = None
                git_cat.return_value.poll.return_value = -1
                commit = self.SO_commit(tags=[f'{tag}: {ref}'])
                self.assertNoReport(self.check, commit)

            # missing and ambiguous object refs
            for status in ('missing', 'ambiguous'):
                self.check._git_cat_file = None
                with patch('subprocess.Popen') as git_cat:
                    git_cat.return_value.poll.return_value = None
                    git_cat.return_value.stdout.readline.return_value = f'{ref} {status}'
                    commit = self.SO_commit(tags=[f'{tag}: {ref}'])
                    r = self.assertReport(self.check, commit)
                    assert isinstance(r, git_mod.InvalidCommitTag)
                    assert f'{status} commit' in r.error

            # valid tag reference
            with patch('subprocess.Popen') as git_cat:
                self.check._git_cat_file = None
                git_cat.return_value.poll.return_value = None
                git_cat.return_value.stdout.readline.return_value = f'{ref} commit 1234'
                commit = self.SO_commit(tags=[f'{tag}: {ref}'])
                self.assertNoReport(self.check, commit)

    def test_summary_length(self):
        self.assertNoReport(self.check, self.SO_commit('single summary headline'))
        self.assertNoReport(self.check, self.SO_commit('a' * 69))
        assert 'no commit message' in \
            self.assertReport(self.check, self.SO_commit('')).error
        assert 'summary is too long' in \
            self.assertReport(self.check, self.SO_commit('a' * 70)).error

    def test_message_body_length(self):
        # message body lines longer than 80 chars are flagged
        long_line = 'a' + ' b' * 40
        assert 'line 2 greater than 80 chars' in \
            self.assertReport(
                self.check,
                self.SO_commit(body=long_line)).error

        # but not non-word lines
        long_line = 'a' * 81
        self.assertNoReport(self.check, self.SO_commit(body=long_line))

    def test_message_empty_lines(self):

        self.assertNoReport(
            self.check,
            FakeCommit(author='author@domain.com', committer='author@domain.com', message="""\
foo

bar

Signed-off-by: author@domain.com
""".splitlines()))

        # missing empty line between summary and body
        assert 'missing empty line before body' in \
            self.assertReport(
                self.check,
                FakeCommit(author='author@domain.com', committer='author@domain.com', message="""\
foo
bar

Signed-off-by: author@domain.com
""".splitlines())).error

        # missing empty line between summary and tags
        assert 'missing empty line before tags' in \
            self.assertReport(
                self.check,
                FakeCommit(author='author@domain.com', committer='author@domain.com', message="""\
foo
Signed-off-by: author@domain.com
""".splitlines())).error

        # missing empty lines between summary, body, and tags
        reports = self.assertReports(
            self.check,
            FakeCommit(author='author@domain.com', committer='author@domain.com', message="""\
foo
bar
Signed-off-by: author@domain.com
""".splitlines()))

        assert 'missing empty line before body' in reports[0].error
        assert 'missing empty line before tags' in reports[1].error

    def test_footer_empty_lines(self):
        for whitespace in ('\t', ' ', ''):
            # empty lines in footer are flagged
            assert 'empty line 4 in footer' in \
                self.assertReport(
                    self.check,
                    FakeCommit(author='author@domain.com', committer='author@domain.com', message=f"""\
foon

blah: dar
{whitespace}
footer: yep
Signed-off-by: author@domain.com
""".splitlines())).error

            # empty lines at the end of a commit message are ignored
            self.assertNoReport(
                self.check,
                FakeCommit(author='author@domain.com', committer='author@domain.com', message=f"""\
foon

blah: dar
footer: yep
Signed-off-by: author@domain.com
{whitespace}
""".splitlines()))

    def test_footer_non_tags(self):
        assert 'non-tag in footer, line 5' in \
            self.assertReport(
                self.check,
                FakeCommit(author='author@domain.com', committer='author@domain.com', message="""\
foon

blah: dar
footer: yep
some random line
Signed-off-by: author@domain.com
""".splitlines())).error


class TestGitPkgCommitsCheck(ReportTestCase):

    check_kls = git_mod.GitPkgCommitsCheck

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, tool, make_repo, make_git_repo):
        self._tool = tool
        self.cache_dir = str(tmp_path)

        # initialize parent repo
        self.parent_git_repo = make_git_repo()
        self.parent_repo = make_repo(
            self.parent_git_repo.path, repo_id='gentoo', arches=['amd64'])
        self.parent_git_repo.add_all('initial commit')
        # create a stub pkg and commit it
        self.parent_repo.create_ebuild('cat/pkg-0')
        self.parent_git_repo.add_all('cat/pkg-0')

        # initialize child repo
        self.child_git_repo = make_git_repo()
        self.child_git_repo.run(['git', 'remote', 'add', 'origin', self.parent_git_repo.path])
        self.child_git_repo.run(['git', 'pull', 'origin', 'master'])
        self.child_git_repo.run(['git', 'remote', 'set-head', 'origin', 'master'])
        self.child_repo = make_repo(self.child_git_repo.path)

    def init_check(self, options=None, future=0):
        self.options = options if options is not None else self._options()
        self.check, required_addons, self.source = init_check(self.check_kls, self.options)
        for k, v in required_addons.items():
            setattr(self, k, v)
        if future:
            self.check.today = datetime.today() + timedelta(days=+future)

    def _options(self, **kwargs):
        args = [
            'scan', '-q', '--cache-dir', self.cache_dir,
            '--repo', self.child_repo.location, '--commits',
        ]
        options, _ = self._tool.parse_args(args)
        return options

    def test_direct_stable(self):
        self.child_repo.create_ebuild('cat/pkg-1', keywords=['amd64'])
        self.child_git_repo.add_all('cat/pkg: version bump to 1')
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.DirectStableKeywords(['amd64'], pkg=CPV('cat/pkg-1'))
        assert r == expected

    def test_direct_no_maintainer(self):
        self.child_repo.create_ebuild('newcat/pkg-1')
        self.child_git_repo.add_all('newcat/pkg: initial import')
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.DirectNoMaintainer(pkg=CPV('newcat/pkg-1'))
        assert r == expected

    def test_bad_commit_summary(self):
        self.child_repo.create_ebuild('cat/pkg-1')
        self.child_git_repo.add_all('version bump')
        commit = self.child_git_repo.HEAD
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.BadCommitSummary(
            'summary missing matching package prefix',
            'version bump', commit=commit)
        assert r == expected

    def test_ebuild_incorrect_copyright(self):
        self.child_repo.create_ebuild('cat/pkg-1')
        line = '# Copyright 1999-2019 Gentoo Authors'
        with open(pjoin(self.child_git_repo.path, 'cat/pkg/pkg-1.ebuild'), 'r+') as f:
            lines = f.read().splitlines()
            lines[0] = line
            f.seek(0)
            f.write('\n'.join(lines))
        self.child_git_repo.add_all('cat/pkg: version bump to 1')
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.EbuildIncorrectCopyright('2019', line=line, pkg=CPV('cat/pkg-1'))
        assert r == expected

    def test_dropped_stable_keywords(self):
        # add stable ebuild to parent repo
        self.parent_repo.create_ebuild('cat/pkg-1', keywords=['amd64'])
        self.parent_git_repo.add_all('cat/pkg: version bump to 1')
        # pull changes and remove it from the child repo
        self.child_git_repo.run(['git', 'pull', 'origin', 'master'])
        self.child_git_repo.remove('cat/pkg/pkg-1.ebuild', msg='cat/pkg: remove 1')
        commit = self.child_git_repo.HEAD
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.DroppedStableKeywords(['amd64'], commit, pkg=CPV('cat/pkg-1'))
        assert r == expected

    def test_dropped_unstable_keywords(self):
        # add stable ebuild to parent repo
        self.parent_repo.create_ebuild('cat/pkg-1', keywords=['~amd64'])
        self.parent_git_repo.add_all('cat/pkg: version bump to 1')
        # pull changes and remove it from the child repo
        self.child_git_repo.run(['git', 'pull', 'origin', 'master'])
        self.child_git_repo.remove('cat/pkg/pkg-1.ebuild', msg='cat/pkg: remove 1')
        commit = self.child_git_repo.HEAD
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.DroppedUnstableKeywords(['~amd64'], commit, pkg=CPV('cat/pkg-1'))
        assert r == expected

    def test_rdepend_change(self):
        # add new pkg to parent repo
        self.parent_repo.create_ebuild('newcat/newpkg-1')
        self.parent_git_repo.add_all('newcat/newpkg: initial import')
        # pull changes to child repo and update previous pkg's RDEPEND
        self.child_git_repo.run(['git', 'pull', 'origin', 'master'])
        with open(pjoin(self.child_git_repo.path, 'cat/pkg/pkg-0.ebuild'), 'a') as f:
            f.write('RDEPEND="newcat/newpkg"\n')
        self.child_git_repo.add_all('cat/pkg: update deps')
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.RdependChange(pkg=CPV('cat/pkg-0'))
        assert r == expected

    def test_missing_slotmove(self):
        # add new ebuild to parent repo
        self.parent_repo.create_ebuild('cat/pkg-1', keywords=['~amd64'])
        self.parent_git_repo.add_all('cat/pkg: version bump to 1')
        # pull changes and modify its slot in the child repo
        self.child_git_repo.run(['git', 'pull', 'origin', 'master'])
        self.child_repo.create_ebuild('cat/pkg-1', keywords=['~amd64'], slot='1')
        self.child_git_repo.add_all('cat/pkg: update SLOT to 1')
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.MissingSlotmove('0', '1', pkg=CPV('cat/pkg-1'))
        assert r == expected

    def test_missing_move(self):
        self.child_git_repo.move('cat', 'newcat', msg='newcat/pkg: moved pkg')
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.MissingMove('cat/pkg', 'newcat/pkg', pkg=CPV('newcat/pkg-0'))
        assert r == expected


class TestGitEclassCommitsCheck(ReportTestCase):

    check_kls = git_mod.GitEclassCommitsCheck

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, tool, make_repo, make_git_repo):
        self._tool = tool
        self.cache_dir = str(tmp_path)

        # initialize parent repo
        self.parent_git_repo = make_git_repo()
        self.parent_repo = make_repo(
            self.parent_git_repo.path, repo_id='gentoo', arches=['amd64'])
        self.parent_git_repo.add_all('initial commit')
        # create a stub eclass and commit it
        touch(pjoin(self.parent_git_repo.path, 'eclass', 'foo.eclass'))
        self.parent_git_repo.add_all('eclass: add foo eclass')

        # initialize child repo
        self.child_git_repo = make_git_repo()
        self.child_git_repo.run(['git', 'remote', 'add', 'origin', self.parent_git_repo.path])
        self.child_git_repo.run(['git', 'pull', 'origin', 'master'])
        self.child_git_repo.run(['git', 'remote', 'set-head', 'origin', 'master'])
        self.child_repo = make_repo(self.child_git_repo.path)

    def init_check(self, options=None, future=0):
        self.options = options if options is not None else self._options()
        self.check, required_addons, self.source = init_check(self.check_kls, self.options)
        for k, v in required_addons.items():
            setattr(self, k, v)
        if future:
            self.check.today = datetime.today() + timedelta(days=+future)

    def _options(self, **kwargs):
        args = [
            'scan', '-q', '--cache-dir', self.cache_dir,
            '--repo', self.child_repo.location, '--commits',
        ]
        options, _ = self._tool.parse_args(args)
        return options

    def test_eclass_incorrect_copyright(self):
        line = '# Copyright 1999-2019 Gentoo Authors'
        with open(pjoin(self.child_git_repo.path, 'eclass/foo.eclass'), 'w') as f:
            f.write(f'{line}\n')
        self.child_git_repo.add_all('eclass: update foo')
        self.init_check()
        r = self.assertReport(self.check, self.source)
        expected = git_mod.EclassIncorrectCopyright('2019', line, eclass='foo')
        assert r == expected

        # correcting the year results in no report
        year = datetime.today().year
        line = f'# Copyright 1999-{year} Gentoo Authors'
        with open(pjoin(self.child_git_repo.path, 'eclass/foo.eclass'), 'w') as f:
            f.write(f'{line}\n')
        self.child_git_repo.add_all('eclass: fix copyright year')
        self.init_check()
        self.assertNoReport(self.check, self.source)
