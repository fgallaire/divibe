import json
import os
import sqlite3
import tempfile
import unittest

from divibe import opencode
from divibe.replay import Replay


def everything(rel):
    return True


class Database:
    """An OpenCode database with the tables and columns Divibe reads."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('create table session (id text, directory text, parent_id text);'
                              'create table message (id text, session_id text, time_created int, data text);'
                              'create table part (id text, message_id text, session_id text, time_created int,'
                              ' data text);')
        self.clock = 1767261600000  # 2026-01-01T10:00:00Z
        self.count = 0

    def session(self, sid, directory, parent=None):
        self.db.execute('insert into session values (?, ?, ?)', (sid, directory, parent))

    def message(self, sid, role, *parts):
        self.count += 1
        mid = f'msg{self.count}'
        self.db.execute('insert into message values (?, ?, ?, ?)', (mid, sid, self.clock, json.dumps({'role': role})))
        for part in parts:
            self.clock += 60000
            self.count += 1
            if part['type'] == 'tool':
                part['state']['time'] = {'start': self.clock, 'end': self.clock + 1000}
            self.db.execute('insert into part values (?, ?, ?, ?, ?)',
                            (f'prt{self.count}', mid, sid, self.clock, json.dumps(part)))

    def tool(self, name, status='completed', **args):
        return {'type': 'tool', 'tool': name, 'state': {'status': status, 'input': args}}

    def save(self):
        self.db.commit()
        self.db.close()


class OpenCodeTest(unittest.TestCase):
    def setUp(self):
        self.project = os.path.join(tempfile.mkdtemp(), 'app')
        self.path = os.path.join(tempfile.mkdtemp(), 'opencode.db')

    def p(self, rel):
        return os.path.join(self.project, rel)

    def test_requests_tools_and_a_subagent_replayed(self):
        d = Database(self.path)
        d.session('s1', self.project)
        d.session('s2', self.project, parent='s1')
        d.session('other', '/elsewhere')
        d.message('s1', 'user', {'type': 'text', 'text': 'add a'},
                  {'type': 'text', 'text': 'the file a.py', 'synthetic': True})
        d.message('s1', 'assistant', {'type': 'text', 'text': 'Adding it.'},
                  d.tool('write', filePath=self.p('a.py'), content='x = 1\ny = 1\n'),
                  d.tool('edit', filePath=self.p('a.py'), oldString='x = 1', newString='x = 2'),
                  d.tool('bash', command='cp a.py b.py', workdir=self.project))
        d.message('s2', 'user', {'type': 'text', 'text': 'write c, as a subagent'})
        d.message('s2', 'assistant', d.tool('edit', filePath='c.py', oldString='', newString='c\n'),
                  d.tool('multiedit', filePath=self.p('a.py'), edits=[
                      {'oldString': 'y = 1', 'newString': 'y = 2'}, {'oldString': 'y = 2', 'newString': 'y = 3'}]),
                  d.tool('write', status='error', filePath=self.p('d.py'), content='d\n'),
                  d.tool('bash', status='error', command='false'))
        d.message('s1', 'user', {'type': 'text', 'text': '/btw done yet?'})
        d.message('other', 'user', {'type': 'text', 'text': 'unrelated'})
        d.save()
        count, items, lines = opencode.timeline(self.project, self.path)
        self.assertEqual(count, 2)
        self.assertEqual(lines, {'opencode s1': 'opencode s1', 'opencode s2': 'opencode s1'})
        self.assertEqual([(i[2], i[1]) for i in items if i[2] in ('request', 'note')],
                         [('request', 'opencode s1'), ('note', 'opencode s1'), ('note', 'opencode s2'),
                          ('note', 'opencode s1')])
        self.assertEqual(items[-2][3][2], True)
        replay = Replay(self.project, everything, repository=False)
        units = replay.run(items, lines, dict)
        self.assertEqual([u['changes'] for u in units], [{'a.py': 'x = 2\ny = 3\n', 'b.py': 'x = 2\ny = 1\n',
                                                          'c.py': 'c\n'}])

    def test_a_session_elsewhere_that_changed_the_project_is_found(self):
        d = Database(self.path)
        d.session('s1', '/elsewhere')
        d.message('s1', 'user', {'type': 'text', 'text': 'fix it there'})
        d.message('s1', 'assistant', d.tool('write', filePath=self.p('a.py'), content='a\n'))
        d.save()
        count, items, _ = opencode.timeline(self.project, self.path)
        self.assertEqual((count, [i[2] for i in items]), (1, ['request', 'edit']))

    def test_no_database_is_no_session(self):
        self.assertEqual(opencode.timeline(self.project, self.path), (0, [], {}))


if __name__ == '__main__':
    unittest.main()
