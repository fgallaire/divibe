import datetime
import os
import tempfile
import unittest

from divibe.build import commits_of, history, moment, plan_dates, tree_files, write


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


if __name__ == '__main__':
    unittest.main()
