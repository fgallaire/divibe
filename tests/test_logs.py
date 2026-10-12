import os
import tempfile
import unittest

from divibe import logs
from tests.fake import Session


class LogsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.project = os.path.join(self.root, 'work', 'app')
        self.projects = os.path.join(self.root, 'projects')

    def read(self, files):
        events, ids = logs.load(files)
        index = logs.backup_index(os.path.join(self.root, 'file-history'))
        return logs.timeline(events, self.project, index), ids

    def test_a_session_that_names_the_project_is_found_and_another_is_not(self):
        one = Session(self.root, '-work-app', self.project)
        one.ask('start')
        one.write(self.project + '/a.py', 'x = 1\n')
        one.save()
        other = Session(self.root, '-work-other', '/elsewhere')
        other.ask('unrelated')
        other.save()
        self.assertEqual(logs.session_files(self.project, self.projects), [one.path])

    def test_a_session_run_from_elsewhere_that_edits_the_project_is_found(self):
        visitor = Session(self.root, '-work-elsewhere', '/elsewhere')
        visitor.ask('fix it there')
        visitor.write(self.project + '/b.py', 'y = 2\n')
        visitor.save()
        self.assertEqual(logs.session_files(self.project, self.projects), [visitor.path])

    def test_a_prefix_of_the_project_name_is_not_the_project(self):
        near = Session(self.root, '-work-application', self.project + 'lication')
        near.write(self.project + 'lication/a.py', 'x\n')
        near.save()
        self.assertEqual(logs.session_files(self.project, self.projects), [])

    def test_the_timeline_holds_requests_notes_edits_and_shell_commands_in_order(self):
        s = Session(self.root, '-work-app', self.project)
        s.ask('add a module')
        s.say('Adding it.')
        s.write(self.project + '/a.py', 'x = 1\n')
        s.bash('sed -i s/1/2/ a.py')
        s.save()
        items, _ = self.read([s.path])
        self.assertEqual([i[2] for i in items], ['request', 'note', 'edit', 'bash'])
        self.assertEqual(items[3][3], ('sed -i s/1/2/ a.py', self.project, False, None))

    def test_a_failed_edit_is_left_out_and_a_failed_command_is_marked(self):
        s = Session(self.root, '-work-app', self.project)
        s.edit(self.project + '/a.py', 'missing', 'x', error=True)
        s.bash('false', error=True)
        s.save()
        items, _ = self.read([s.path])
        self.assertEqual([(i[2], i[3][2] if i[2] == 'bash' else None) for i in items], [('bash', True)])

    def test_a_shell_put_back_in_its_directory_says_where(self):
        s = Session(self.root, '-work-app', self.project)
        s.bash('cd /tmp', result=f'Shell cwd was reset to {self.project}')
        s.save()
        items, _ = self.read([s.path])
        self.assertEqual(items[0][3][3], self.project)

    def test_tool_results_compactions_and_reminders_are_no_requests(self):
        s = Session(self.root, '-work-app', self.project)
        s.add({'type': 'user', 'isCompactSummary': True, 'message': {'content': 'summary'}})
        s.add({'type': 'user', 'message': {'content': '<system-reminder>x</system-reminder>'}})
        s.add({'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'go on'}]}})
        s.save()
        items, _ = self.read([s.path])
        self.assertEqual([(i[2], i[3]) for i in items], [('request', 'go on')])

    def test_a_request_with_the_id_of_a_snapshot_is_kept(self):
        # Claude Code writes the snapshot for a prompt under the prompt's id,
        # just before the prompt itself.
        s = Session(self.root, '-work-app', self.project)
        s.add({'type': 'file-history-snapshot', 'messageId': 'shared', 'snapshot': {'trackedFileBackups': {}}})
        s.add({'type': 'user', 'uuid': 'shared', 'message': {'content': 'the request'}})
        s.save()
        items, _ = self.read([s.path])
        self.assertEqual([i[3] for i in items if i[2] == 'request'], ['the request'])

    def test_a_backup_of_a_project_file_is_on_the_timeline_and_one_of_another_file_is_not(self):
        s = Session(self.root, '-work-app', self.project)
        s.backup(self.project + '/a.py', 'x = 0\n')
        s.backup('/elsewhere/z.py', 'z\n')
        s.save()
        items, _ = self.read([s.path])
        backups = [i[3] for i in items if i[2] == 'backup']
        self.assertEqual([path for path, _ in backups], [self.project + '/a.py'])
        with open(backups[0][1]) as f:
            self.assertEqual(f.read(), 'x = 0\n')

    def test_a_continuation_copies_events_and_belongs_to_the_first_session(self):
        first = Session(self.root, '-work-app', self.project, name='aaaa')
        first.ask('one')
        first.save()
        second = Session(self.root, '-work-app', self.project, name='bbbb', start='2026-01-01T11:00:00')
        second.events.append(dict(first.events[0]))
        second.ask('two')
        second.save()
        events, ids = logs.load([second.path, first.path])
        self.assertEqual([e['_session'] for e in events], ['aaaa', 'bbbb'])
        self.assertEqual(logs.lineages(ids), {'aaaa': 'aaaa', 'bbbb': 'aaaa'})


if __name__ == '__main__':
    unittest.main()
