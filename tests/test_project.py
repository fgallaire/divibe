import os
import subprocess
import tempfile
import unittest

from divibe.project import Project, Rules


def write(root, rel, content=''):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)


class RulesTest(unittest.TestCase):
    def test_a_name_matches_at_any_depth_and_a_slash_anchors_it(self):
        rules = Rules([('', 'secret.txt'), ('', '/build'), ('', 'docs/private/')])
        self.assertTrue(rules.ignored('secret.txt'))
        self.assertTrue(rules.ignored('a/b/secret.txt'))
        self.assertTrue(rules.ignored('build/x.js'))
        self.assertFalse(rules.ignored('src/build/x.js'))
        self.assertTrue(rules.ignored('docs/private/a.md'))
        self.assertFalse(rules.ignored('docs/public/a.md'))

    def test_a_folder_pattern_does_not_match_a_file_of_that_name(self):
        rules = Rules([('', 'cache/')])
        self.assertTrue(rules.ignored('cache/a'))
        self.assertFalse(rules.ignored('cache'))

    def test_a_negation_takes_a_path_back(self):
        rules = Rules([('', '*.log'), ('', '!keep.log')])
        self.assertTrue(rules.ignored('a.log'))
        self.assertFalse(rules.ignored('keep.log'))

    def test_a_nested_gitignore_speaks_for_its_folder_only(self):
        rules = Rules([('sub', '*.tmp')])
        self.assertTrue(rules.ignored('sub/a.tmp'))
        self.assertFalse(rules.ignored('a.tmp'))


class ProjectTest(unittest.TestCase):
    def test_without_git_the_usual_folders_and_the_gitignore_are_left_out(self):
        root = tempfile.mkdtemp()
        write(root, 'a.py', 'a')
        write(root, 'node_modules/x/index.js', 'x')
        write(root, '.gitignore', 'private/\n')
        write(root, 'private/notes.md', 'n')
        project = Project(root)
        self.assertFalse(project.git)
        self.assertEqual(sorted(project.files()), ['.gitignore', 'a.py'])

    def test_in_git_what_git_ignores_is_out_and_a_tracked_file_stays_in(self):
        root = tempfile.mkdtemp()
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        write(root, 'kept.log', 'tracked before the rule')
        subprocess.run(['git', 'add', 'kept.log'], cwd=root, check=True)
        write(root, '.gitignore', '*.log\n')
        write(root, 'other.log', 'ignored')
        write(root, '.git/info/exclude', 'audits/\n')
        write(root, 'audits/a.md', 'private')
        write(root, 'm.py', 'm')
        project = Project(root)
        self.assertTrue(project.git)
        self.assertEqual(sorted(project.files()), ['.gitignore', 'kept.log', 'm.py'])
        self.assertFalse(project.public('other.log'))
        self.assertFalse(project.public('audits/b.md'))

    def test_in_git_what_no_one_commits_is_out_though_nothing_ignores_it(self):
        root = tempfile.mkdtemp()
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        write(root, 'm.py', 'm')
        write(root, '__pycache__/m.cpython-314.pyc', 'compiled')
        write(root, 'vendored/node_modules/x.js', 'x')
        subprocess.run(['git', 'add', '-f', 'vendored/node_modules/x.js'], cwd=root, check=True)
        project = Project(root)
        self.assertEqual(sorted(project.files()), ['m.py', 'vendored/node_modules/x.js'])

    def test_an_exclude_given_by_the_user_is_out(self):
        root = tempfile.mkdtemp()
        write(root, 'a.py', 'a')
        write(root, 'notes/me.md', 'me')
        project = Project(root, excludes=['notes/'])
        self.assertEqual(sorted(project.files()), ['a.py'])


if __name__ == '__main__':
    unittest.main()
