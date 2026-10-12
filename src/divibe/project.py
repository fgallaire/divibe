"""The project's files as they stand, and which paths belong in its history.

In a git repository, a path belongs if git does not ignore it: the project's
own `.gitignore`, `.git/info/exclude` and the user's global excludes decide,
as they decide what `git add` takes. Elsewhere, a few folders no one commits
are left out, and the `.gitignore` files are read with the common rules.
"""
import fnmatch
import os
import re
import subprocess

DEFAULT_EXCLUDES = ['.git/', 'node_modules/', '__pycache__/', '*.pyc', '.venv/', 'venv/', 'dist/', 'build/',
                    '.DS_Store', '*.swp', '.svelte-kit/', 'coverage/', '.cache/']


def is_git(root):
    return subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=root, capture_output=True, text=True,
                          check=False).stdout.strip() == os.path.realpath(root)


REFLOG_COMMIT = re.compile(r'HEAD@\{(\d+)\}\0([0-9a-f]+)\0commit(?: \((\w+)\))?: (.*)')


def commits_made(root):
    """The commits the reflog of HEAD records, oldest first: (unix time,
    amend, subject, hash). Git keeps them 90 days unless told otherwise."""
    out = subprocess.run(['git', 'log', '-g', '--date=unix', '--format=%gd%x00%H%x00%gs', 'HEAD'], cwd=root,
                         capture_output=True, text=True, check=False).stdout
    found = []
    for line in out.splitlines():
        m = REFLOG_COMMIT.fullmatch(line)
        if m:
            found.append((int(m.group(1)), m.group(3) == 'amend', m.group(4), m.group(2)))
    return found[::-1]


class Rules:
    """Ignore patterns, read the way .gitignore reads them: a pattern with a
    slash anywhere but at its end is anchored to its folder, one ending with
    a slash names a folder, `!` takes a path back."""

    def __init__(self, patterns=()):
        self.rules = []
        for base, pattern in patterns:
            self.add(base, pattern)

    def add(self, base, pattern):
        pattern = pattern.rstrip('\n').rstrip()
        if not pattern or pattern.startswith('#'):
            return
        negate = pattern.startswith('!')
        pattern = pattern[1:] if negate else pattern
        folder = pattern.endswith('/')
        pattern = pattern.rstrip('/')
        anchored = '/' in pattern
        self.rules.append((base, pattern.lstrip('/'), folder, anchored, negate))

    def ignored(self, rel):
        parts = rel.split('/')
        verdict = False
        for base, pattern, folder, anchored, negate in self.rules:
            if base and not rel.startswith(base + '/'):
                continue
            inner = rel[len(base) + 1:] if base else rel
            names = inner.split('/')
            hit = False
            # a folder pattern matches any folder on the path; a file pattern
            # the path itself or one of the folders holding it
            for depth in range(1, len(names) + 1):
                if folder and depth == len(names):
                    break
                candidate = '/'.join(names[:depth]) if anchored else names[depth - 1]
                if fnmatch.fnmatchcase(candidate, pattern) or (anchored and fnmatch.fnmatchcase(
                        candidate, pattern.replace('**/', ''))):
                    hit = True
                    break
            if hit:
                verdict = not negate
        return verdict or any(p in ('.git',) for p in parts)


def read_rules(root, extra=()):
    rules = Rules([('', p) for p in DEFAULT_EXCLUDES])
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules')]
        if '.gitignore' in names:
            base = os.path.relpath(folder, root)
            with open(os.path.join(folder, '.gitignore'), encoding='utf-8', errors='replace') as f:
                for line in f:
                    rules.add('' if base == '.' else base, line)
    exclude = os.path.join(root, '.git', 'info', 'exclude')
    if os.path.exists(exclude):
        with open(exclude, encoding='utf-8', errors='replace') as f:
            for line in f:
                rules.add('', line)
    for pattern in extra:
        rules.add('', pattern)
    return rules


def git_ignored(root, paths):
    """The paths, among those given, that git ignores in this repository; a
    tracked file is never one of them, whatever the patterns say."""
    if not paths:
        return set()
    out = subprocess.run(['git', 'check-ignore', '--stdin', '-z'], cwd=root,
                         input='\0'.join(paths) + '\0', capture_output=True, text=True, check=False).stdout
    return {p for p in out.split('\0') if p}


class Project:
    """The project at root: its files as they stand, and whether a path
    belongs in its history."""

    def __init__(self, root, excludes=()):
        self.root = os.path.realpath(root)
        self.git = is_git(self.root)
        self.rules = read_rules(self.root, excludes)
        self.extra = Rules([('', p) for p in excludes])
        # what no one commits, even where nothing says to ignore it
        self.usual = Rules([('', p) for p in DEFAULT_EXCLUDES])
        self.tracked = set()
        self.made = commits_made(self.root) if self.git else []
        if self.git:
            out = subprocess.run(['git', '-c', 'core.quotepath=false', 'ls-files', '-z'], cwd=self.root,
                                 capture_output=True, text=True, check=False).stdout
            self.tracked = {p for p in out.split('\0') if p}
        self._verdicts = {}

    def verdict(self, rel, ignored_by_git):
        if self.extra.ignored(rel) or rel.split('/')[0] == '.git':
            return False
        if rel in self.tracked:
            return True
        return not (ignored_by_git or self.usual.ignored(rel))

    def public(self, rel):
        if rel not in self._verdicts:
            self.decide([rel])
        return self._verdicts[rel]

    def decide(self, paths):
        """Settles many paths at once, so that public() answers from memory."""
        todo = [p for p in dict.fromkeys(paths) if p not in self._verdicts]
        if not todo:
            return
        if self.git:
            ignored = git_ignored(self.root, todo)
            for p in todo:
                self._verdicts[p] = self.verdict(p, p in ignored)
        else:
            for p in todo:
                self._verdicts[p] = not self.rules.ignored(p)

    def subject_of(self, revision):
        """The subject of the commit a revision names, if git still has it."""
        out = subprocess.run(['git', 'log', '-1', '--format=%s', revision, '--'], cwd=self.root,
                             capture_output=True, text=True, check=False)
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None

    def commit_files(self, commit, leave=()):
        """The files of one of the project's commits that belong in its
        history, but those in leave, with their content."""
        out = subprocess.run(['git', '-c', 'core.quotepath=false', 'ls-tree', '-r', '-z', '--name-only', commit],
                             cwd=self.root, capture_output=True, text=True, check=False).stdout
        names = [n for n in out.split('\0') if n and n not in leave]
        self.decide(names)
        return {n: subprocess.run(['git', 'show', f'{commit}:{n}'], cwd=self.root, capture_output=True,
                                  check=False).stdout.decode('utf-8', 'surrogateescape')
                for n in names if self.public(n)}

    def files(self, folder=None):
        """The files of the project (or of a copy of it in folder) that belong
        in its history, with their content."""
        folder = folder or self.root
        found = {}
        for base, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules')]
            for name in names:
                path = os.path.join(base, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                found[os.path.relpath(path, folder)] = path
        if folder == self.root:
            # a tracked file is the project's, in whatever folder the walk skips
            for rel in self.tracked:
                path = os.path.join(folder, rel)
                if rel not in found and os.path.isfile(path) and not os.path.islink(path):
                    found[rel] = path
        self.decide(list(found))
        result = {}
        for rel, path in found.items():
            if self.public(rel):
                with open(path, encoding='utf-8', errors='surrogateescape', newline='') as f:
                    result[rel] = f.read()
        return result
