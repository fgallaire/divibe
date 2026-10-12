"""The shell commands that changed project files, replayed on an in-memory
tree: `sed -i`, `cat > file <<EOF`, `echo` or `printf` into a file, `cp`, `mv`,
`rm`, `touch`, `git mv`, `git rm`, `npm pkg set`, and a Python script given on
standard input (`python3 - <<EOF`), whose file accesses are turned to the
tree. Nothing is written to disk and nothing is run but that script, whose
subprocesses, removals and renames are refused.

A command that may change files in a way not replayed here (a build, a
package manager, a script on disk, a git command that rewrites the work tree)
is reported as unknown: what it did is taken from the next backup or copy.
"""
import builtins
import fnmatch
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess


class Tree:
    """The project's files by path relative to its root, and the directory a
    command runs in."""

    def __init__(self, root, files=None):
        self.root = root.rstrip('/')
        self.files = {} if files is None else files
        self.cwd = self.root

    def rel(self, path, cwd=None):
        """The path relative to the root, '' for the root itself, None for a
        path outside it or one still holding a shell variable."""
        path = str(path)
        if '$' in path or '$' in (cwd or self.cwd):
            return None
        full = os.path.normpath(os.path.join(cwd or self.cwd, os.path.expanduser(path)))
        if full == self.root:
            return ''
        return full[len(self.root) + 1:] if full.startswith(self.root + '/') else None

    def glob(self, pattern):
        rel = self.rel(pattern)
        if rel is None:
            return []
        if any(ch in rel for ch in '*?['):
            return [k for k in self.files if fnmatch.fnmatch(k, rel)]
        return [k for k in self.files if k == rel or k.startswith(rel + '/')] if rel else list(self.files)

    def inside(self):
        return self.cwd == self.root or self.cwd.startswith(self.root + '/')


# --- sed -------------------------------------------------------------------

def sed_expression(expr, extended=False):
    """A GNU sed `s` command as a function of a line, None if not one."""
    if not expr.startswith('s') or len(expr) < 4:
        return None
    d = expr[1]
    parts, cur, i = [], '', 2
    while i < len(expr):
        ch = expr[i]
        if ch == '\\' and i + 1 < len(expr):
            nxt = expr[i + 1]
            cur += nxt if nxt == d else ch + nxt
            i += 2
            continue
        if ch == d:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
        i += 1
    parts.append(cur)
    if len(parts) < 3:
        return None
    pattern, replacement, flags = parts[0], parts[1], parts[2]
    # in a basic expression, \( \) \{ \} \+ \? \| are the operators
    py = pattern if extended else ''
    j = len(pattern) if extended else 0
    while j < len(pattern):
        ch = pattern[j]
        if ch == '\\' and j + 1 < len(pattern):
            nxt = pattern[j + 1]
            py += nxt if nxt in '(){}+?|' else '\\' + nxt
            j += 2
            continue
        py += '\\' + ch if ch in '(){}+?|' else ch
        j += 1
    rep = re.sub(r'\\(\d)', r'\\g<\1>', replacement.replace('&', r'\g<0>')).replace('\\n', '\n')
    count = 0 if 'g' in flags else 1
    try:
        compiled = re.compile(py)
        compiled.sub(rep, '')
    except re.error:
        return None
    return lambda line: compiled.sub(rep, line, count=count)


def run_sed(tree, words):
    args = words[1:]
    exprs, targets, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == '-e' and i + 1 < len(args):
            exprs.append(args[i + 1])
            i += 2
            continue
        if a.startswith('-'):
            i += 1
            continue
        if not exprs:
            exprs.append(a)
        else:
            targets.append(a)
        i += 1
    extended = any(a in ('-E', '-r') or (a.startswith('-') and not a.startswith('--') and ('E' in a or 'r' in a))
                   for a in args)
    subs = [sed_expression(e, extended) for e in exprs]
    if not subs or None in subs:
        return False
    for t in targets:
        for rel in tree.glob(t):
            lines = tree.files[rel].split('\n')
            for sub in subs:
                lines = [sub(line) for line in lines]
            tree.files[rel] = '\n'.join(lines)
    return True


# --- a Python script on standard input -------------------------------------

def refused(*args, **kwargs):
    raise PermissionError('refused while replaying')


def run_python(tree, source):
    """Runs a script with open(), pathlib, os.walk, os.listdir and glob turned
    to the tree, and whatever would act on the machine refused. True if it
    ran to its end."""
    real_open = builtins.open
    # a file left open, `open(p, 'w').write(s)`, is written when the script
    # ends rather than whenever the collector gets to it
    unclosed = []

    def fake_open(path, mode='r', *a, **k):
        rel = tree.rel(str(path))
        if rel is None:
            # never a write outside the project: the replay writes nothing
            if any(m in mode for m in 'wax+'):
                raise PermissionError(path)
            return real_open(path, mode, *a, **k)
        if 'w' in mode or 'a' in mode or 'x' in mode:
            buffer = io.StringIO() if 'b' not in mode else io.BytesIO()
            if 'a' in mode:
                start = tree.files.get(rel, '')
                buffer.write(start.encode() if 'b' in mode else start)
            close = buffer.close
            done = []

            def closing():
                # once: the collector closes the buffer again, at any later
                # time, and would write back what it held then
                if done:
                    return
                done.append(True)
                value = buffer.getvalue()
                tree.files[rel] = value.decode() if isinstance(value, bytes) else value
                close()

            buffer.close = closing
            unclosed.append(closing)
            return buffer
        if rel not in tree.files:
            raise FileNotFoundError(path)
        return io.StringIO(tree.files[rel]) if 'b' not in mode else io.BytesIO(tree.files[rel].encode())

    class FakePath:
        def __init__(self, *parts):
            self.p = os.path.join(*[str(x) for x in parts]) if parts else '.'

        def __truediv__(self, other):
            return FakePath(self.p, str(other))

        def __str__(self):
            return self.p

        __fspath__ = __str__

        def __repr__(self):
            return f'FakePath({self.p!r})'

        name = property(lambda self: os.path.basename(self.p))
        parent = property(lambda self: FakePath(os.path.dirname(self.p) or '.'))
        suffix = property(lambda self: os.path.splitext(self.p)[1])
        stem = property(lambda self: os.path.splitext(os.path.basename(self.p))[0])

        def read_text(self, *a, **k):
            with fake_open(self.p) as f:
                return f.read()

        def read_bytes(self):
            return self.read_text().encode()

        def write_text(self, data, *a, **k):
            rel = tree.rel(self.p)
            if rel is None:
                raise PermissionError(self.p)
            tree.files[rel] = data
            return len(data)

        def exists(self):
            rel = tree.rel(self.p)
            if rel is None:
                return os.path.exists(self.p)
            return rel in tree.files or any(k.startswith(rel + '/') for k in tree.files)

        def is_file(self):
            return tree.rel(self.p) in tree.files

        def unlink(self, missing_ok=False):
            rel = tree.rel(self.p)
            if rel is None:
                raise PermissionError(self.p)
            tree.files.pop(rel, None)

        def glob(self, pattern):
            base = tree.rel(self.p)
            prefix = (base + '/') if base else ''
            return [FakePath(tree.root + '/' + k) for k in tree.files
                    if k.startswith(prefix) and fnmatch.fnmatch(k[len(prefix):], pattern)]

        rglob = glob

    import glob as globmod
    real = {'listdir': os.listdir, 'walk': os.walk, 'exists': os.path.exists, 'isfile': os.path.isfile,
            'isdir': os.path.isdir, 'glob': globmod.glob, 'iglob': globmod.iglob}

    # what a script sees of the project's folders is the tree, never the
    # disk as it stands today
    def inside_dir(path):
        rel = tree.rel(str(path))
        return rel

    def listdir(path='.'):
        rel = inside_dir(path)
        if rel is None:
            return real['listdir'](path)
        prefix = (rel + '/') if rel else ''
        return sorted({k[len(prefix):].split('/')[0] for k in tree.files if k.startswith(prefix)})

    def walk(top, *a, **k):
        rel = inside_dir(top)
        if rel is None:
            yield from real['walk'](top, *a, **k)
            return
        prefix = (rel + '/') if rel else ''
        folders = {}
        for key in tree.files:
            if key.startswith(prefix):
                parts = key[len(prefix):].split('/')
                for depth in range(len(parts)):
                    folders.setdefault('/'.join(parts[:depth]), (set(), set()))
                    if depth == len(parts) - 1:
                        folders['/'.join(parts[:depth])][1].add(parts[depth])
                    else:
                        folders['/'.join(parts[:depth])][0].add(parts[depth])
        for sub in sorted(folders):
            dirs, files = folders[sub]
            yield (os.path.join(str(top), sub) if sub else str(top)), sorted(dirs), sorted(files)

    def exists(path):
        rel = inside_dir(path)
        if rel is None:
            return real['exists'](path)
        return rel in tree.files or any(k.startswith(rel + '/') for k in tree.files) or rel == ''

    def isfile(path):
        rel = inside_dir(path)
        return real['isfile'](path) if rel is None else rel in tree.files

    def isdir(path):
        rel = inside_dir(path)
        if rel is None:
            return real['isdir'](path)
        return rel == '' or any(k.startswith(rel + '/') for k in tree.files)

    def globbed(pattern, *a, **k):
        rel = inside_dir(pattern)
        if rel is None:
            return real['glob'](pattern, *a, **k)
        hits = sorted(key for key in tree.files if fnmatch.fnmatch(key, rel))
        if os.path.isabs(str(pattern)):
            return [tree.root + '/' + h for h in hits]
        base = tree.rel(tree.cwd) or ''
        return [h[len(base) + 1:] if base and h.startswith(base + '/') else h for h in hits]

    stubs = [(os, name) for name in ('system', 'remove', 'unlink', 'rename', 'replace', 'rmdir', 'removedirs',
                                     'makedirs', 'mkdir', 'chdir', 'popen', 'execv', 'execvp', 'kill')]
    # a script that moves into a folder moves the tree's own directory
    moves = {'chdir': lambda path: setattr(tree, 'cwd', os.path.normpath(os.path.join(tree.cwd, str(path)))),
             'makedirs': lambda *a, **k: None, 'mkdir': lambda *a, **k: None}
    stubs += [(shutil, name) for name in ('copy', 'copy2', 'copyfile', 'copytree', 'move', 'rmtree')]
    stubs += [(subprocess, name) for name in ('run', 'call', 'check_call', 'check_output', 'Popen')]
    saved = [(module, name, getattr(module, name)) for module, name in stubs if hasattr(module, name)]
    real_path = pathlib.Path
    try:
        builtins.open = fake_open
        pathlib.Path = FakePath
        for module, name, _ in saved:
            setattr(module, name, moves.get(name, refused) if module is os else refused)
        os.listdir, os.walk = listdir, walk
        os.path.exists, os.path.isfile, os.path.isdir = exists, isfile, isdir
        globmod.glob, globmod.iglob = globbed, lambda *a, **k: iter(globbed(*a, **k))
        exec(compile(source, '<log>', 'exec'), {'__name__': '__main__', 'open': fake_open, 'Path': FakePath})
        return True
    except BaseException:
        return False
    finally:
        for closing in unclosed:
            closing()
        builtins.open = real_open
        pathlib.Path = real_path
        for module, name, value in saved:
            setattr(module, name, value)
        os.listdir, os.walk = real['listdir'], real['walk']
        os.path.exists, os.path.isfile, os.path.isdir = real['exists'], real['isfile'], real['isdir']
        globmod.glob, globmod.iglob = real['glob'], real['iglob']


# --- a command line --------------------------------------------------------

HEREDOC_CAT = re.compile(r"cat\s+(>>?)\s*(\S+)\s+<<-?\s*'?\"?(\w+)'?\"?\n(.*?)\n\3[ \t]*(?:\n|$)", re.S)
HEREDOC_CAT2 = re.compile(r"cat\s+<<-?\s*'?\"?(\w+)'?\"?\s*(>>?)\s*(\S+)\n(.*?)\n\1[ \t]*(?:\n|$)", re.S)
HEREDOC_PY = re.compile(r"python3?\s+-\s+<<-?\s*'?\"?(\w+)'?\"?[^\n]*\n(.*?)\n\1[ \t]*(?:\n|$)", re.S)

# commands that may change files in ways not replayed here
GIT_WRITERS = {'checkout', 'switch', 'reset', 'restore', 'stash', 'apply', 'am', 'merge', 'rebase', 'cherry-pick',
               'pull', 'clean', 'revert'}
OPAQUE = {'perl', 'tee', 'awk', 'ed', 'patch', 'truncate', 'dd', 'rsync', 'tar', 'unzip', 'ln', 'make', 'cargo',
          'npm', 'npx', 'pnpm', 'yarn', 'bun', 'node', 'deno', 'python', 'python3', 'pip', 'uv', 'go', 'prettier',
          'eslint', 'black', 'ruff'}
# what reads and runs tests without writing files anyone tracks
QUIET = {'npm': {'test', 'run', 'view', 'ls', 'pack', 'whoami', 'outdated'}, 'npx': {'vitest', 'tsc', 'jest'},
         'node': {'--test', '--version'}, 'python': {'--version'}, 'python3': {'--version'}}
QUIET_MODULES = {'unittest', 'pytest', 'doctest', 'pydoc', 'json.tool', 'py_compile'}


def quiet(words):
    if words[0] in ('python', 'python3') and words[1:2] == ['-m'] and len(words) > 2:
        return words[2] in QUIET_MODULES
    return len(words) > 1 and words[1] in QUIET.get(words[0], ())


def run_bash(tree, command, cwd):
    """Applies what a command did to the tree. Returns the parts of it that
    may have changed project files without being replayed."""
    unknown = []
    tree.cwd = cwd

    def cwd_at(index):
        # the directory at a point of the command: every `cd` written before
        here = cwd
        for m in re.finditer(r'(?:^|[;&|\n]\s*)cd\s+([^\s;&|]+)', command[:index]):
            here = os.path.normpath(os.path.join(here, os.path.expanduser(m.group(1).strip('"\''))))
        return here

    writes = []
    for m in HEREDOC_CAT.finditer(command):
        writes.append((m.start(), 'file', m.group(2), m.group(4), m.group(1) == '>>'))
    for m in HEREDOC_CAT2.finditer(command):
        writes.append((m.start(), 'file', m.group(3), m.group(4), m.group(2) == '>>'))
    for m in HEREDOC_PY.finditer(command):
        writes.append((m.start(), 'python', None, m.group(2), False))
    for at, kind, target, body, append in sorted(writes):
        tree.cwd = cwd_at(at)
        if kind == 'file':
            rel = tree.rel(target.strip('"\''))
            if rel is not None:
                tree.files[rel] = (tree.files.get(rel, '') if append else '') + body + '\n'
        elif tree.inside() or tree.root in body:
            if not run_python(tree, body):
                unknown.append(body.split('\n')[0][:80])
    plain = HEREDOC_PY.sub('', HEREDOC_CAT2.sub('', HEREDOC_CAT.sub('', command)))
    tree.cwd = cwd
    for part in re.split(r'\s*(?:;|&&|\|\||\n)\s*', plain):
        part = part.strip()
        if not part:
            continue
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            continue
        if not words:
            continue
        head = words[0]
        redirect = None
        if head not in ('echo', 'printf'):
            # a redirection is no argument: `2>/dev/null`, `> log`, `< input`
            kept, pending = [], None
            for w in words:
                if pending is not None:
                    if pending != '<' and not w.startswith('&'):
                        redirect = w
                    pending = None
                elif re.fullmatch(r'\d*>>?|&>>?|<', w):
                    pending = w
                elif re.match(r'^(\d*>>?|&>>?)', w):
                    target = re.sub(r'^(\d*>>?|&>>?)', '', w)
                    if target and not target.startswith('&'):
                        redirect = target
                elif not w.startswith('<'):
                    kept.append(w)
            words = kept
            if not words:
                continue
            head = words[0]
        if head == 'cd' and len(words) == 2:
            tree.cwd = os.path.normpath(os.path.join(tree.cwd, os.path.expanduser(words[1])))
        elif head == 'sed' and '-i' in ' '.join(words[1:3]):
            if not run_sed(tree, words):
                unknown.append(part)
        elif head in ('echo', 'printf') and ('>' in words or '>>' in words):
            at = words.index('>') if '>' in words else words.index('>>')
            rel = tree.rel(words[at + 1]) if at + 1 < len(words) else None
            if rel is not None:
                text = ' '.join(words[1:at])
                text = (text + '\n') if head == 'echo' else text.encode().decode('unicode_escape')
                tree.files[rel] = (tree.files.get(rel, '') + text) if words[at] == '>>' else text
        elif head == 'rm' or words[:2] == ['git', 'rm']:
            for w in words[(2 if head == 'git' else 1):]:
                if not w.startswith('-'):
                    for rel in tree.glob(w):
                        del tree.files[rel]
        elif head == 'touch':
            for w in words[1:]:
                rel = tree.rel(w)
                if not w.startswith('-') and rel and rel not in tree.files:
                    tree.files[rel] = ''
        elif head == 'mv' or words[:2] == ['git', 'mv']:
            names = [w for w in words[(2 if head == 'git' else 1):] if not w.startswith('-')]
            if len(names) < 2 or tree.rel(names[-1]) is None:
                continue
            dst = tree.rel(names[-1])
            into = len(names) > 2 or names[-1].endswith('/') or any(k.startswith(dst + '/') for k in tree.files)
            for src in names[:-1]:
                base = tree.rel(src)
                if base is None:
                    unknown.append(part)
                    continue
                for rel in tree.glob(src):
                    if into:
                        # a file, or a directory moved whole, lands under dst
                        top = os.path.dirname(base) if not any(ch in base for ch in '*?[') else os.path.dirname(rel)
                        tail = rel[len(top) + 1:] if top else rel
                        tree.files[(dst + '/' if dst else '') + tail] = tree.files.pop(rel)
                    else:
                        tree.files[dst + rel[len(base):]] = tree.files.pop(rel)
        elif head == 'cp':
            names = [w for w in words[1:] if not w.startswith('-')]
            if len(names) >= 2 and tree.rel(names[-1]) is not None:
                target = tree.rel(names[-1])
                into = len(names) > 2 or names[-1].endswith('/') or any(k.startswith(target + '/') for k in tree.files)
                for src in names[:-1]:
                    srel = tree.rel(src)
                    if srel is None or srel not in tree.files:
                        # a file from outside: its content then is not known here
                        unknown.append(part)
                        continue
                    tree.files[(target + '/' + os.path.basename(src)) if into else target] = tree.files[srel]
        elif head == 'npm' and words[1:3] == ['pkg', 'set'] and 'package.json' in tree.files:
            data = json.loads(tree.files['package.json'])
            for pair in words[3:]:
                if '=' in pair and not pair.startswith('-'):
                    key, value = pair.split('=', 1)
                    node = data
                    keys = key.split('.')
                    for k in keys[:-1]:
                        node = node.setdefault(k, {})
                    node[keys[-1]] = value
            tree.files['package.json'] = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
        elif redirect is not None and tree.rel(redirect) is not None:
            unknown.append(part)
        elif tree.inside():
            if head == 'git' and len(words) > 1 and words[1] in GIT_WRITERS:
                unknown.append(part)
            elif head in OPAQUE and len(words) > 1 and not quiet(words):
                unknown.append(part)
    tree.cwd = cwd
    return unknown
