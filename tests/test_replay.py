import os
import re
import tempfile
import unittest

from divibe import logs
from divibe.replay import Replay, git_operations, seconds
from tests.fake import Session


def everything(rel):
    return True


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.project = os.path.join(self.root, 'work', 'app')
        self.copies = {}

    def session(self, **kw):
        return Session(self.root, '-work-app', self.project, **kw)

    def replay(self, *sessions, checkpoints=(), markers=None, made=()):
        files = [s.save() for s in sessions]
        events, ids = logs.load(files)
        index = logs.backup_index(os.path.join(self.root, 'file-history'))
        items = logs.timeline(events, self.project, index)
        r = Replay(self.project, everything, checkpoints, markers, made=made)
        units = r.run(items, logs.lineages(ids), lambda folder: self.copies[folder])
        return r, units

    def p(self, rel):
        return os.path.join(self.project, rel)

    def test_one_unit_per_request_with_what_its_files_became(self):
        s = self.session()
        s.ask('add a')
        s.write(self.p('a.py'), 'x = 1\n')
        s.edit(self.p('a.py'), '1', '2')
        s.ask('add b')
        s.write(self.p('b.py'), 'y\n')
        _, units = self.replay(s)
        self.assertEqual([u['request'] for u in units], ['add a', 'add b'])
        self.assertEqual(units[0]['changes'], {'a.py': 'x = 2\n'})
        self.assertEqual(units[1]['changes'], {'b.py': 'y\n'})

    def test_a_request_that_changes_nothing_makes_no_unit(self):
        s = self.session()
        s.ask('just look')
        s.bash('ls')
        s.ask('add a')
        s.write(self.p('a.py'), 'x\n')
        _, units = self.replay(s)
        self.assertEqual([u['request'] for u in units], ['add a'])

    def test_a_file_changed_and_put_back_makes_no_unit(self):
        s = self.session()
        s.ask('try')
        s.write(self.p('a.py'), 'x\n')
        s.bash('rm a.py')
        _, units = self.replay(s)
        self.assertEqual(units, [])

    def test_a_commit_the_agent_made_cuts_the_work_with_its_message_and_what_it_staged(self):
        s = self.session()
        s.ask('build it and commit as you go')
        s.write(self.p('a.py'), 'a\n')
        s.bash('git add -A && git commit -q -m "Add a"')
        s.write(self.p('b.py'), 'b\n')
        s.write(self.p('c.py'), 'c\n')
        s.bash('git add b.py && git commit -m "Add b"')
        r, units = self.replay(s)
        self.assertEqual([u['agent']['message'] for u in units if u['agent']], ['Add a', 'Add b'])
        self.assertEqual([sorted(u['changes']) for u in units], [['a.py'], ['b.py'], ['c.py']])
        self.assertEqual(r.stats['agent commits'], 2)

    def test_a_commit_in_another_repository_is_no_cut(self):
        s = self.session()
        s.ask('work')
        s.write(self.p('a.py'), 'a\n')
        s.bash('cd /elsewhere && git commit -am "other"')
        s.write(self.p('b.py'), 'b\n')
        _, units = self.replay(s)
        self.assertEqual([sorted(u['changes']) for u in units], [['a.py', 'b.py']])

    def test_a_commit_in_a_project_that_is_no_repository_is_no_cut(self):
        s = self.session()
        s.ask('work')
        s.write(self.p('a.py'), 'a\n')
        s.bash('git commit -am "nothing" | tail -1')
        s.write(self.p('b.py'), 'b\n')
        files = [s.save()]
        events, ids = logs.load(files)
        items = logs.timeline(events, self.project, {})
        units = Replay(self.project, everything, repository=False).run(items, logs.lineages(ids), dict)
        self.assertEqual([sorted(u['changes']) for u in units], [['a.py', 'b.py']])

    def test_a_commit_the_reflog_shows_made_while_its_command_ran_counts_though_the_command_failed(self):
        s = self.session()
        s.ask('work')
        s.write(self.p('a.py'), 'a\n')
        s.bash('git add -A && git commit -q -m "Add a" && false', error=True)
        s.write(self.p('b.py'), 'b\n')
        s.bash('git add -A && git commit -q -m "Add b" && false', error=True)
        # 'Add b' was made later, by a command the logs do not hold
        made = [(seconds('2026-01-01T10:04:30Z'), False, 'Add a'), (seconds('2026-01-01T10:20:00Z'), False, 'Add b')]
        _, units = self.replay(s, made=made)
        self.assertEqual([(u['agent'] or {}).get('message') for u in units], ['Add a', None])
        self.assertEqual([sorted(u['changes']) for u in units], [['a.py'], ['b.py']])
        self.assertEqual(units[0]['date'], '2026-01-01T10:04:30.000Z')

    def test_a_commit_takes_the_moment_the_reflog_gives_it(self):
        s = self.session()
        s.ask('work')
        s.write(self.p('a.py'), 'a\n')
        s.bash('python -m unittest && git add -A && git commit -q -m "Add a"')
        s.write(self.p('b.py'), 'b\n')
        s.bash('git add -A && git commit -q -m "Add b"')
        made = [(seconds('2026-01-01T10:04:50Z'), False, 'Add a')]
        _, units = self.replay(s, made=made)
        self.assertEqual([u['date'] for u in units], ['2026-01-01T10:04:50.000Z', '2026-01-01T10:08:00.000Z'])

    def test_a_failed_commit_is_no_cut(self):
        s = self.session()
        s.ask('work')
        s.write(self.p('a.py'), 'a\n')
        s.bash('git commit -m "nothing staged"', error=True)
        s.write(self.p('b.py'), 'b\n')
        _, units = self.replay(s)
        self.assertEqual(len(units), 1)

    def test_a_backup_corrects_a_file_the_replay_got_wrong(self):
        s = self.session()
        s.ask('work')
        s.write(self.p('a.py'), 'x = 1\n')
        s.bash('node rewrite.mjs')
        s.backup(self.p('a.py'), 'x = 9\n')
        s.edit(self.p('a.py'), '9', '10')
        r, units = self.replay(s)
        self.assertEqual(units[0]['changes'], {'a.py': 'x = 10\n'})
        self.assertEqual(r.stats['backups correcting the replay'], 1)
        self.assertEqual(r.stats['shell commands not replayed'], 1)

    def test_an_edit_that_does_not_apply_is_counted(self):
        s = self.session()
        s.ask('work')
        s.write(self.p('a.py'), 'x = 1\n')
        s.edit(self.p('a.py'), 'absent', 'y')
        r, _ = self.replay(s)
        self.assertEqual(r.stats['edits that did not apply'], 1)

    def test_the_current_tree_is_the_last_unit_when_the_replay_ends_elsewhere(self):
        s = self.session()
        s.ask('work')
        s.write(self.p('a.py'), 'x = 1\n')
        self.copies['now'] = {'a.py': 'x = 2\n', 'b.py': 'b\n'}
        r, units = self.replay(s, checkpoints=[('2099-01-01T00:00:00Z', 'now', 'the current tree')])
        self.assertEqual(units[-1]['changes'], {'a.py': 'x = 2\n', 'b.py': 'b\n'})
        self.assertEqual(r.drift[-1][3], ['a.py', 'b.py'])

    def test_a_copy_speaks_only_for_the_folders_it_holds(self):
        s = self.session()
        s.ask('work')
        s.write(self.p('docs/a.md'), 'a\n')
        s.write(self.p('src/m.py'), 'm\n')
        self.copies['old'] = {'src/m.py': 'm\n'}
        r, units = self.replay(s, checkpoints=[('2099-01-01T00:00:00Z', 'old', 'a copy')])
        self.assertEqual(r.drift[-1][3], [])

    def test_a_note_naming_another_defect_cuts_when_markers_are_given(self):
        s = self.session()
        s.ask('fix the audit')
        s.say('F1 first.')
        s.write(self.p('a.py'), 'a\n')
        s.say('F1 is fixed. Now F2.')
        s.write(self.p('b.py'), 'b\n')
        _, units = self.replay(s, markers=re.compile(r'\b(F\d)\b'))
        self.assertEqual([(u['marker'], sorted(u['changes'])) for u in units], [('F1', ['a.py']), ('F2', ['b.py'])])

    def test_two_sessions_working_at_once_keep_their_units_apart(self):
        one = self.session(name='one')
        two = self.session(name='two')
        one.ask('a')
        two.ask('b')
        one.write(self.p('a.py'), 'a\n')
        two.write(self.p('b.py'), 'b\n')
        _, units = self.replay(one, two)
        self.assertEqual(sorted((u['request'], sorted(u['changes'])) for u in units), [('a', ['a.py']), ('b', ['b.py'])])


class GitOperationsTest(unittest.TestCase):
    def test_the_message_of_a_commit_in_the_project(self):
        ops = git_operations('cd /work/app && git add -A && git commit -q -m "One line"', '/elsewhere', '/work/app')
        self.assertEqual(ops, [('add', None), ('commit', 'One line', False, False)])

    def test_a_commit_made_with_minus_C(self):
        ops = git_operations('git -C /work/app commit -am "Fix"', '/elsewhere', '/work/app')
        self.assertEqual(ops, [('commit', 'Fix', False, True)])

    def test_an_amend(self):
        ops = git_operations('git commit --amend --no-edit', '/work/app', '/work/app')
        self.assertEqual(ops, [('commit', None, True, False)])


if __name__ == '__main__':
    unittest.main()
