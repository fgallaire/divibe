import unittest

from divibe.shell import Tree, run_bash

ROOT = '/work/app'


def tree(**files):
    t = Tree(ROOT)
    t.files.update({k.replace('__', '/').replace('_dot_', '.'): v for k, v in files.items()})
    return t


class ShellTest(unittest.TestCase):
    def test_sed_in_place_with_a_basic_and_an_extended_expression(self):
        t = tree(**{'a_dot_py': 'x = 1\ny = 1\n'})
        self.assertEqual(run_bash(t, "sed -i 's/1/2/' a.py", ROOT), [])
        self.assertEqual(t.files['a.py'], 'x = 2\ny = 2\n')
        run_bash(t, "sed -i -E 's/(x|y) = 2/\\1 = 3/g' a.py", ROOT)
        self.assertEqual(t.files['a.py'], 'x = 3\ny = 3\n')

    def test_a_heredoc_written_by_cat_lands_where_the_cd_before_it_leads(self):
        t = tree()
        run_bash(t, "cd src && cat > m.py <<'EOF'\nprint(1)\nEOF", ROOT)
        self.assertEqual(t.files, {'src/m.py': 'print(1)\n'})

    def test_echo_and_printf_into_a_file(self):
        t = tree()
        run_bash(t, 'echo hello > a.txt; echo more >> a.txt; printf "x\\ny" > b.txt', ROOT)
        self.assertEqual(t.files, {'a.txt': 'hello\nmore\n', 'b.txt': 'x\ny'})

    def test_move_copy_remove_and_touch(self):
        t = tree(**{'a_dot_py': 'a', 'd__b_dot_py': 'b'})
        run_bash(t, 'mv a.py c.py && cp c.py e.py && mv d lib && rm e.py && touch new.py', ROOT)
        self.assertEqual(t.files, {'c.py': 'a', 'lib/b.py': 'b', 'new.py': ''})

    def test_moving_files_into_a_folder(self):
        t = tree(**{'a_dot_py': 'a', 'b_dot_py': 'b', 'lib__x_dot_py': 'x'})
        run_bash(t, 'git mv a.py b.py lib/', ROOT)
        self.assertEqual(sorted(t.files), ['lib/a.py', 'lib/b.py', 'lib/x.py'])

    def test_a_python_script_on_standard_input_edits_the_tree(self):
        t = tree(**{'a_dot_py': 'old\n'})
        script = "python3 - <<'PY'\np = 'a.py'\ns = open(p).read()\nopen(p, 'w').write(s.replace('old', 'new'))\nPY"
        self.assertEqual(run_bash(t, script, ROOT), [])
        self.assertEqual(t.files['a.py'], 'new\n')

    def test_a_script_sees_the_tree_and_not_the_disk(self):
        t = tree(**{'src__a_dot_py': '1', 'src__b_dot_py': '2'})
        script = ("python3 - <<'PY'\nimport os\nnames = sorted(os.listdir('src'))\n"
                  "open('names.txt', 'w').write(','.join(names))\nPY")
        run_bash(t, script, ROOT)
        self.assertEqual(t.files['names.txt'], 'a.py,b.py')

    def test_a_script_that_fails_is_reported_and_keeps_what_it_wrote_before(self):
        t = tree()
        script = "python3 - <<'PY'\nopen('a.txt', 'w').write('done')\nraise SystemExit(1)\nPY"
        self.assertEqual(len(run_bash(t, script, ROOT)), 1)
        self.assertEqual(t.files['a.txt'], 'done')

    def test_a_script_cannot_run_a_command_or_write_outside(self):
        t = tree()
        script = "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['touch', '/tmp/x'])\nPY"
        self.assertEqual(len(run_bash(t, script, ROOT)), 1)
        self.assertEqual(t.files, {})

    def test_a_file_outside_the_project_is_not_touched(self):
        t = tree()
        run_bash(t, 'echo x > /tmp/elsewhere.txt; touch ../other.py', ROOT)
        self.assertEqual(t.files, {})

    def test_a_path_with_a_shell_variable_is_left_alone(self):
        t = tree(**{'a_dot_py': 'a'})
        run_bash(t, 'rm $FILE', ROOT)
        self.assertEqual(t.files, {'a.py': 'a'})

    def test_commands_that_write_in_ways_not_replayed_are_reported(self):
        t = tree()
        for command in ('npm install', 'git checkout -- a.py', 'node build.mjs', 'ls > list.txt', 'perl -pi -e s/a/b/ x'):
            self.assertEqual(run_bash(t, command, ROOT), [command], command)

    def test_commands_that_only_read_report_nothing(self):
        t = tree()
        for command in ('ls -la', 'git status', 'git log --oneline', 'npm test', 'grep -rn x . 2>/dev/null',
                        'npx vitest run', 'cat a.py 2>&1', 'git commit -m "x"'):
            self.assertEqual(run_bash(t, command, ROOT), [], command)

    def test_a_command_run_outside_the_project_reports_nothing(self):
        t = tree()
        self.assertEqual(run_bash(t, 'npm install', '/elsewhere'), [])


if __name__ == '__main__':
    unittest.main()
