import contextlib
import io
import os
import subprocess
import tempfile
import unittest

from divibe import build
from divibe.cli import main
from tests.fake import Session


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = main(list(args))
    return status, out.getvalue()


class CommandLineTest(unittest.TestCase):
    """A project made by one session in two requests, the second committed by
    the agent, then the whole way through: survey, units, messages, build."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.project = os.path.join(self.root, 'app')
        os.makedirs(self.project)
        subprocess.run(['git', 'init', '-q'], cwd=self.project, check=True)
        with open(os.path.join(self.project, 'a.py'), 'w') as f:
            f.write('x = 2\n')
        with open(os.path.join(self.project, 'b.py'), 'w') as f:
            f.write('y\n')
        s = Session(self.root, '-app', self.project)
        s.ask('add a')
        s.write(self.project + '/a.py', 'x = 1\n')
        s.edit(self.project + '/a.py', '1', '2')
        s.ask('add b and commit')
        s.write(self.project + '/b.py', 'y\n')
        s.bash('git add -A && git commit -q -m "Add b"')
        s.save()
        self.state = os.path.join(self.root, 'state')
        self.options = ['--logs', os.path.join(self.root, 'projects'),
                        '--backups', os.path.join(self.root, 'file-history'), '--state', self.state]

    def test_the_whole_way_through(self):
        status, out = run('survey', self.project, *self.options)
        self.assertIn('requests             2 that changed the project', out)
        self.assertIn('Expected quality: exact', out)
        status, out = run('units', self.project, '--todo', *self.options)
        # the agent committed after the first request: it goes into that commit
        self.assertIn('0 without a message', out)
        repo = os.path.join(self.root, 'out')
        status, out = run('build', self.project, '--out', repo, *self.options)
        self.assertIn('the last one is the project as it stands', out)
        self.assertEqual([r[0] for r in build.history(repo)], ['Add b'])

    def test_messages_recorded_by_rank(self):
        s = Session(self.root, '-app', self.project, name='later', start='2026-01-02T10:00:00')
        s.ask('touch up a')
        s.edit(self.project + '/a.py', '2', '3')
        s.save()
        with open(os.path.join(self.project, 'a.py'), 'w') as f:
            f.write('x = 3\n')
        run('units', self.project, *self.options)
        status, out = run('units', self.project, '--todo', *self.options)
        self.assertIn('[2]', out)
        self.assertIn('1 without a message', out)
        tsv = os.path.join(self.root, 'messages.tsv')
        with open(tsv, 'w') as f:
            f.write('2\tTouch up a\n')
        status, out = run('messages', self.project, tsv, '--state', self.state)
        self.assertIn('1 messages recorded', out)
        repo = os.path.join(self.root, 'out')
        run('build', self.project, '--out', repo, *self.options)
        self.assertEqual([r[0] for r in build.history(repo)], ['Add b', 'Touch up a'])

    def test_verify_compares_with_the_project_own_history(self):
        subprocess.run(['git', 'add', '-A'], cwd=self.project, check=True)
        env = dict(os.environ, GIT_AUTHOR_NAME='A', GIT_AUTHOR_EMAIL='a@b', GIT_COMMITTER_NAME='A',
                   GIT_COMMITTER_EMAIL='a@b')
        subprocess.run(['git', 'commit', '-q', '-m', 'Add b'], cwd=self.project, check=True, env=env)
        status, out = run('verify', self.project, *self.options)
        self.assertEqual(status, 0, out)
        self.assertIn('1 of 1 commits identical', out)


if __name__ == '__main__':
    unittest.main()
