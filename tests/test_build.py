import datetime
import os
import tempfile
import unittest

from divibe.build import (commits_of, history, merge3, moment, plan_dates, tags, tags_of, tree_files, version_of,
                          write)


def unit(uid, date, changes, request='r', agent=None):
    return {'id': uid, 'date': date, 'changes': changes, 'request': request, 'request_id': request,
            'agent': agent}


class CommitsTest(unittest.TestCase):
    def test_folding_dropping_and_the_agent_message(self):
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'a': 'a'}),
            unit('2', '2026-01-01T10:05:00Z', {'b': 'b'}),
            unit('3', '2026-01-01T10:06:00Z', {'c': 'c'}),
            unit('4', '2026-01-01T10:07:00Z', {'c': None}),
            unit('5', '2026-01-01T10:08:00Z', {'d': 'd'}, agent={'message': 'Add d', 'amend': False}),
        ]
        commits, missing = commits_of(units, {'1': '+', '2': 'Add a and b', '3': '-', '4': '-'})
        self.assertEqual(missing, [])
        self.assertEqual([(c[1], c[5]) for c in commits], [('Add a and b', {'a': 'a', 'b': 'b'}),
                                                           ('Add d', {'d': 'd'})])

    def test_a_unit_with_no_message_is_reported_or_drafted(self):
        units = [unit('1', '2026-01-01T10:00:00Z', {'a': 'a'}, request='fais le module\nvite')]
        self.assertEqual(commits_of(units, {})[1], ['1'])
        self.assertEqual(commits_of(units, {}, draft=True)[0][0][1], 'Draft: fais le module')

    def test_work_cut_off_before_a_commit_of_the_agent_goes_into_it(self):
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'a': 'a'}),
            unit('2', '2026-01-01T10:05:00Z', {'b': 'b'}, agent={'message': 'Add a and b', 'amend': False}),
            unit('3', '2026-01-01T10:06:00Z', {'c': 'c'}),
        ]
        commits, missing = commits_of(units, {})
        self.assertEqual([(c[1], c[5]) for c in commits], [('Add a and b', {'a': 'a', 'b': 'b'})])
        self.assertEqual(missing, ['3'])

    def test_an_amend_folds_into_the_commit_before(self):
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'a': 'a'}, agent={'message': 'Add a', 'amend': False}),
            unit('2', '2026-01-01T10:02:00Z', {'a': 'a2'}, agent={'message': None, 'amend': True}),
        ]
        commits, _ = commits_of(units, {})
        self.assertEqual([(c[1], c[5]) for c in commits], [('Add a', {'a': 'a2'})])
        self.assertEqual(commits[0][2], moment('2026-01-01T10:00:00Z'))

    def test_an_amend_with_no_message_folds_back_even_with_a_commit_after_it(self):
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'a': 'a'}, agent={'message': 'Add a', 'amend': False}),
            unit('2', '2026-01-01T10:02:00Z', {'a': 'a2'}, agent={'message': None, 'amend': True}),
            unit('3', '2026-01-01T10:04:00Z', {'b': 'b'}, agent={'message': 'Add b', 'amend': False}),
        ]
        commits, _ = commits_of(units, {})
        self.assertEqual([(c[1], c[5]) for c in commits], [('Add a', {'a': 'a2'}), ('Add b', {'b': 'b'})])

    def test_commits_taken_back_by_a_soft_reset_go_into_the_next_one(self):
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'a': 'a'}, agent={'message': 'Add a', 'amend': False}),
            unit('2', '2026-01-01T10:02:00Z', {'b': 'b'}, agent={'message': 'Add b', 'amend': False}),
            unit('3', '2026-01-01T10:04:00Z', {'a': 'a2'}, agent={'message': 'Fix a', 'amend': False}),
            # git reset --soft HEAD~1 && git commit --amend --no-edit
            unit('4', '2026-01-01T10:06:00Z', {}, agent={'message': '', 'amend': True, 'uncommit': 1}),
        ]
        commits, _ = commits_of(units, {})
        self.assertEqual([(c[1], c[5], c[2]) for c in commits],
                         [('Add a', {'a': 'a'}, moment('2026-01-01T10:00:00Z')),
                          ('Add b', {'b': 'b', 'a': 'a2'}, moment('2026-01-01T10:02:00Z'))])

    def test_a_squashed_fixup_folds_into_the_commit_it_names(self):
        fixup = {'message': 'fixup! Add a', 'amend': False}
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'a': 'a', 'c': 'c'}, agent={'message': 'Add a', 'amend': False}),
            unit('2', '2026-01-01T10:02:00Z', {'b': 'b'}, agent={'message': 'Add b', 'amend': False}),
            unit('3', '2026-01-01T10:04:00Z', {'a': 'a2', 'b': 'b2'}, agent=dict(fixup, squashed=True)),
            unit('4', '2026-01-01T10:06:00Z', {'d': 'd'}, agent=fixup),
        ]
        commits, _ = commits_of(units, {})
        # b changed after 'Add a' too: its fixed state starts where it was
        self.assertEqual([(c[1], c[5]) for c in commits], [('Add a', {'a': 'a2', 'c': 'c'}),
                                                           ('Add b', {'b': 'b2'}),
                                                           ('fixup! Add a', {'d': 'd'})])
        self.assertEqual(commits[0][2], moment('2026-01-01T10:00:00Z'))

    def test_a_later_change_that_changed_nothing_does_not_hold_a_fixup_back(self):
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'v': '0.4'}, agent={'message': 'Package', 'amend': False}),
            unit('2', '2026-01-01T10:02:00Z', {'b': 'b'}, agent={'message': 'Add b', 'amend': False}),
            # a bump to 0.5 the agent took back by an amend
            unit('3', '2026-01-01T10:04:00Z', {'v': '0.4'}, agent={'message': None, 'amend': True}),
            unit('4', '2026-01-01T10:06:00Z', {'v': '0.3'},
                 agent={'message': 'fixup! Package', 'amend': False, 'squashed': True}),
        ]
        commits, _ = commits_of(units, {})
        self.assertEqual([(c[1], c[5]) for c in commits], [('Package', {'v': '0.3'}), ('Add b', {'b': 'b'})])


class TagsTest(unittest.TestCase):
    def test_the_agent_tags_and_the_versions_its_files_give(self):
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'a': 'a'}, agent={'message': 'Add a', 'amend': False}),
            unit('2', '2026-01-01T10:02:00Z', {'pyproject.toml': '[project]\nname = "x"\nversion = "0.3.0"\n'},
                 agent={'message': 'Package', 'amend': False}),
            unit('3', '2026-01-01T10:04:00Z', {'b': 'b'}, agent={'message': 'Add b', 'amend': False}),
            unit('4', '2026-01-01T10:06:00Z', {'pyproject.toml': '[project]\nversion = "0.4.0"\n[tool.x]\n'
                                                                'version = "9"\n'},
                 agent={'message': 'Bump', 'amend': False}),
        ]
        commits, _ = commits_of(units, {})
        events = [('tag', 1, 'v0.1', 'Add a', 'First', '2026-01-01T10:01:00.000Z'),
                  ('tag', 3, 'v0.4.0', 'Add b', 'Moved', '2026-01-01T10:05:00.000Z'),
                  ('tag', 3, 'v0.2', 'Add b', None, '2026-01-01T10:05:00.000Z'), ('untag', 3, 'v0.2'),
                  # a tag can only name a commit made before it
                  ('tag', 1, 'early', 'Add b', None, '2026-01-01T10:01:00.000Z')]
        self.assertEqual(tags_of(units, commits, events), {
            'v0.1': ('1', 'First', '2026-01-01T10:01:00.000Z'), 'v0.3.0': ('2', None, None),
            'v0.4.0': ('3', 'Moved', '2026-01-01T10:05:00.000Z')})

    def test_a_version_file_gives_no_version_for_a_private_package(self):
        self.assertEqual(version_of('package.json', '{"name": "app", "version": "1.2.0"}'), '1.2.0')
        self.assertIsNone(version_of('package.json', '{"version": "1.2.0", "private": true}'))
        self.assertEqual(version_of('Cargo.toml', '[package]\nname = "x"\nversion = "2.0.1"\n'), '2.0.1')
        self.assertIsNone(version_of('pyproject.toml', '[project]\nname = "x"\ndynamic = ["version"]\n'))
        self.assertIsNone(version_of('pyproject.toml', '[project]\nversion = "0.0.0"\n'))


class FixupTest(unittest.TestCase):
    def test_a_fixup_of_a_file_later_commits_changed_merges_into_each_of_them(self):
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'R': 'intro\nold end\n'}, agent={'message': 'Start', 'amend': False}),
            unit('2', '2026-01-01T10:02:00Z', {'L': 'MIT\n'}, agent={'message': 'Add the license', 'amend': False}),
            unit('3', '2026-01-01T10:04:00Z', {'R': 'intro\nnew end\n'}, agent={'message': 'Reword', 'amend': False}),
            unit('4', '2026-01-01T10:06:00Z', {'R': 'intro\nnew end\n\n## License\n'},
                 agent={'message': 'fixup! Add the license', 'amend': False, 'squashed': True}),
        ]
        commits, _ = commits_of(units, {})
        self.assertEqual([(c[1], c[5]) for c in commits], [
            ('Start', {'R': 'intro\nold end\n'}),
            ('Add the license', {'L': 'MIT\n', 'R': 'intro\nold end\n\n## License\n'}),
            ('Reword', {'R': 'intro\nnew end\n\n## License\n'})])

    def test_a_fixup_that_cannot_merge_goes_into_the_last_commit(self):
        units = [
            unit('1', '2026-01-01T10:00:00Z', {'R': 'a\nb\n'}, agent={'message': 'Start', 'amend': False}),
            unit('2', '2026-01-01T10:02:00Z', {'R': 'a\nB\n'}, agent={'message': 'Change b', 'amend': False}),
            unit('3', '2026-01-01T10:04:00Z', {'R': 'a\nbee\n'},
                 agent={'message': 'fixup! Start', 'amend': False, 'squashed': True}),
        ]
        commits, _ = commits_of(units, {})
        self.assertEqual([(c[1], c[5]) for c in commits], [('Start', {'R': 'a\nb\n'}), ('Change b', {'R': 'a\nbee\n'})])

    def test_the_three_way_merge(self):
        self.assertEqual(merge3('a\nb\nc\n', 'a\nB\nc\n', 'x\na\nb\nc\ny\n'), 'x\na\nB\nc\ny\n')
        self.assertEqual(merge3('a\nb\n', 'z\na\nb\n', 'a\nb2\n'), 'z\na\nb2\n')
        self.assertIsNone(merge3('a\nb\n', 'a\nB\n', 'a\nb2\n'))


class DatesTest(unittest.TestCase):
    def commit(self, key, when, group, fixed=False):
        return (key, key, moment(when), group, fixed, {})

    def test_commits_of_one_request_made_at_once_are_spread_out_after_their_work(self):
        commits = [self.commit('a', '2026-01-01T10:00:00Z', 'r1'), self.commit('b', '2026-01-01T10:00:00Z', 'r1'),
                   self.commit('c', '2026-01-01T10:00:30Z', 'r1'), self.commit('d', '2026-01-01T12:00:00Z', 'r2')]
        dates = plan_dates(commits)
        times = [dates[k] for k in 'abcd']
        self.assertEqual(times[0], moment('2026-01-01T10:00:00Z'))
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:3])]
        self.assertTrue(all(90 <= g <= 330 for g in gaps), gaps)
        self.assertEqual(times[3], moment('2026-01-01T12:00:00Z'))

    def test_spread_commits_stay_before_the_next_request(self):
        commits = [self.commit(k, '2026-01-01T10:00:00Z', 'r1') for k in 'abcdef']
        commits.append(self.commit('next', '2026-01-01T10:05:00Z', 'r2'))
        dates = plan_dates(commits)
        self.assertLess(dates['f'], dates['next'])
        self.assertEqual(len(set(dates.values())), len(dates))

    def test_a_commit_the_agent_made_keeps_its_moment(self):
        commits = [self.commit('a', '2026-01-01T10:00:00Z', 'r1', fixed=True),
                   self.commit('b', '2026-01-01T10:00:20Z', 'r1', fixed=True)]
        dates = plan_dates(commits)
        self.assertEqual(dates['b'], moment('2026-01-01T10:00:20Z'))

    def test_two_commits_the_agent_made_in_one_second_keep_it(self):
        commits = [self.commit('a', '2026-01-01T10:00:20Z', 'r1', fixed=True),
                   self.commit('b', '2026-01-01T10:00:20Z', 'r1', fixed=True),
                   self.commit('c', '2026-01-01T10:00:20Z', 'r1')]
        dates = plan_dates(commits)
        self.assertEqual([dates[k] for k in 'ab'], [moment('2026-01-01T10:00:20Z')] * 2)
        self.assertGreater(dates['c'], dates['b'])

    def test_no_commit_is_dated_before_its_work(self):
        commits = [self.commit('a', '2026-01-01T10:00:00Z', 'r1'), self.commit('b', '2026-01-01T11:00:00Z', 'r1')]
        dates = plan_dates(commits)
        self.assertEqual(dates['b'], moment('2026-01-01T11:00:00Z'))


class WriteTest(unittest.TestCase):
    def test_the_repository_holds_the_commits_with_their_dates_and_notes(self):
        repo = os.path.join(tempfile.mkdtemp(), 'out')
        commits = [('1', 'Initial commit', None, 'r', True, {'a.py': 'a\n'}),
                   ('2', 'Add b, drop a', None, 'r', True, {'b/c.py': 'c\n', 'a.py': None})]
        dates = {'1': moment('2026-01-01T10:00:00Z'), '2': moment('2026-01-01T10:10:00Z')}
        made = write(repo, commits, dates, ('Ada', 'ada@example.org'), notes={'2': 'the request'},
                     timezone=datetime.timezone.utc)
        self.assertEqual(made, 2)
        self.assertEqual(tree_files(repo), {'b/c.py': 'c\n'})
        rows = history(repo)
        self.assertEqual([r[0] for r in rows], ['Initial commit', 'Add b, drop a'])
        self.assertEqual(moment(rows[1][2]), moment('2026-01-01T10:10:00Z'))

    def test_tags_go_on_their_commits_and_one_that_changes_nothing_is_the_one_before(self):
        repo = os.path.join(tempfile.mkdtemp(), 'out')
        commits = [('1', 'One', None, 'r', True, {'a.py': 'a\n'}), ('2', 'Same', None, 'r', True, {'a.py': 'a\n'}),
                   ('3', 'Two', None, 'r', True, {'b.py': 'b\n'})]
        dates = {k: moment(f'2026-01-01T10:0{k}:00Z') for k in '123'}
        write(repo, commits, dates, ('Ada', 'ada@example.org'), timezone=datetime.timezone.utc,
              tags={'v1': ('2', 'First', '2026-01-01T10:05:00.000Z'), 'v2': ('3', None, None)})
        self.assertEqual(tags(repo), {'v1': 1, 'v2': 2})


if __name__ == '__main__':
    unittest.main()
