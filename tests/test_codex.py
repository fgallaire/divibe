import json
import os
import tempfile
import unittest

from divibe import codex
from divibe.replay import Replay, apply_diff


def everything(rel):
    return True


class Rollout:
    """A Codex session file, item by item, a minute apart."""

    def __init__(self, home, project, name='s1'):
        self.path = os.path.join(home, 'sessions', '2026', '01', '01', f'rollout-{name}.jsonl')
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.minute = 0
        self.lines = [{'timestamp': '2026-01-01T10:00:00.000Z', 'type': 'session_meta',
                       'payload': {'id': name, 'cwd': project}}]

    def item(self, **item):
        self.minute += 1
        self.lines.append({'timestamp': f'2026-01-01T10:{self.minute:02d}:00.000Z', 'type': 'event_msg',
                           'payload': {'type': 'item_completed', 'item': item}})

    def save(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.writelines(json.dumps(line) + '\n' for line in self.lines)
        return self.path


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.project = os.path.join(tempfile.mkdtemp(), 'app')

    def test_requests_commands_and_file_changes_replayed(self):
        r = Rollout(self.home, self.project)
        r.item(type='UserMessage', id='u1', content=[{'type': 'text', 'text': 'add a and b'}])
        r.item(type='AgentMessage', id='m1', content=[{'type': 'Text', 'text': 'Adding them.'}])
        r.item(type='FileChange', id='f1', status='completed', changes={
            self.project + '/a.py': {'type': 'add', 'content': 'one\ntwo\nthree\n'},
            self.project + '/b.py': {'type': 'add', 'content': 'b\n'}})
        r.item(type='FileChange', id='f2', status='completed', changes={
            self.project + '/a.py': {'type': 'update', 'unified_diff': '@@ -2,2 +2,3 @@\n two\n-three\n+3\n+four\n',
                                     'move_path': self.project + '/lib/a.py'},
            self.project + '/b.py': {'type': 'delete', 'content': 'b\n'}})
        r.item(type='CommandExecution', id='c1', command=['/bin/bash', '-lc', 'cp lib/a.py c.py'],
               cwd='file://' + self.project, status='completed', exit_code=0, duration={'secs': 2, 'nanos': 0})
        r.item(type='CommandExecution', id='c2', command=['/bin/bash', '-lc', 'false'], cwd='file://' + self.project,
               status='failed', exit_code=1, duration={'secs': 0, 'nanos': 0})
        # a forked session copies the items it was forked from
        fork = Rollout(self.home, self.project, name='s2')
        fork.lines += r.lines[1:3]
        files = [r.save(), fork.save()]
        self.assertEqual(codex.session_files(self.project, self.home), sorted(files))
        items = codex.timeline(sorted(files))
        self.assertEqual([i[2] for i in items], ['request', 'note', 'edit', 'edit', 'edit', 'edit', 'bash', 'bash'])
        self.assertEqual(items[6][0], '2026-01-01T10:04:58.000Z')
        self.assertEqual(items[6][3], ('cp lib/a.py c.py', self.project, False, None, '2026-01-01T10:05:00.000Z'))
        self.assertTrue(items[7][3][2])
        replay = Replay(self.project, everything, repository=False)
        units = replay.run(items, {}, dict)
        # a.py and b.py, added and gone within the unit, are no change of it
        self.assertEqual(units[0]['changes'], {'lib/a.py': 'one\ntwo\n3\nfour\n', 'c.py': 'one\ntwo\n3\nfour\n'})
        self.assertEqual(replay.stats['patches'], 4)

    def test_a_patch_on_a_file_the_replay_has_otherwise_is_counted(self):
        r = Rollout(self.home, self.project)
        r.item(type='UserMessage', id='u1', content=[{'type': 'text', 'text': 'fix a'}])
        r.item(type='FileChange', id='f1', status='completed', changes={
            self.project + '/a.py': {'type': 'add', 'content': 'x = 1\n'}})
        r.item(type='FileChange', id='f2', status='completed', changes={
            self.project + '/a.py': {'type': 'update', 'unified_diff': '@@ -1 +1 @@\n-x = 2\n+x = 3\n'}})
        replay = Replay(self.project, everything, repository=False)
        replay.run(codex.timeline([r.save()]), {}, dict)
        self.assertEqual(replay.stats['patches that did not apply'], 1)


class DiffTest(unittest.TestCase):
    def test_a_hunk_goes_where_its_old_lines_are_when_the_numbers_moved(self):
        self.assertEqual(apply_diff('a\nb\nc\nd\n', '@@ -1,2 +1,2 @@\n c\n-d\n+D\n'), 'a\nb\nc\nD\n')

    def test_an_insertion_with_no_context(self):
        self.assertEqual(apply_diff('a\nb\n', '@@ -1,0 +2,1 @@\n+x\n'), 'a\nx\nb\n')

    def test_the_end_of_a_file_with_no_newline(self):
        self.assertEqual(apply_diff('a\nb', '@@ -2 +2 @@\n-b\n\\ No newline at end of file\n+c\n'), 'a\nc\n')
        self.assertEqual(apply_diff('a\n', '@@ -1 +1 @@\n-a\n+z\n\\ No newline at end of file\n'), 'z')

    def test_a_hunk_whose_old_lines_are_not_there_does_not_apply(self):
        self.assertIsNone(apply_diff('a\nb\n', '@@ -1 +1 @@\n-q\n+r\n'))


if __name__ == '__main__':
    unittest.main()
