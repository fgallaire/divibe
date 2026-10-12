"""The work replayed in time on an in-memory tree, and cut into units.

A unit is the work of one request: what the files became once the agent was
done with it. Within a request, a `git commit` the agent made in the project
is a cut of its own, with its message and the files it staged; so is a note
naming another defect, when markers are given.

Each unit holds the full content of the files it changed (None for a file
removed), so a later step may fold units together or drop one without
replaying again.
"""
import collections
import datetime
import os
import re
import shlex

from .shell import Tree, run_bash


class Tracked(dict):
    """The tree's files, remembering which ones a step changed."""

    def __init__(self):
        super().__init__()
        self.dirty = set()

    def __setitem__(self, key, value):
        if self.get(key) != value:
            self.dirty.add(key)
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self.dirty.add(key)
        super().__delitem__(key)

    def pop(self, key, *default):
        if key in self:
            self.dirty.add(key)
        return super().pop(key, *default)


def stat(before, after):
    """Lines added and removed from one content to the other."""
    a = (before or '').split('\n') if before is not None else []
    b = (after or '').split('\n') if after is not None else []
    counts = collections.Counter(a)
    counts.subtract(b)
    return sum(-n for n in counts.values() if n < 0), sum(n for n in counts.values() if n > 0)


def seconds(moment):
    """A log's moment as a unix time."""
    return datetime.datetime.fromisoformat(moment.replace('Z', '+00:00')).timestamp()


def utc(unix):
    """A unix time as a log's moment."""
    return datetime.datetime.fromtimestamp(unix, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def git_operations(command, cwd, root):
    """What a command line run in the project stages, commits, squashes and
    tags: ('add', paths or None for everything) for `git add`, `git rm` and
    `git mv`, ('commit', message, amend, all_tracked), ('autosquash',) for
    `git rebase --autosquash`, and those of tag_operations, in order."""
    found = []
    here = cwd
    for part in re.split(r'\s*(?:;|&&|\|\||\n)\s*', command):
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            continue
        while words and re.match(r'[A-Za-z_]\w*=', words[0]):
            words = words[1:]
        if not words:
            continue
        if words[0] == 'cd' and len(words) == 2:
            here = os.path.normpath(os.path.join(here, os.path.expanduser(words[1])))
            continue
        if words[0] != 'git':
            continue
        args, repo = words[1:], here
        while args and args[0] in ('-C', '-c'):
            if args[0] == '-C' and len(args) > 1:
                repo = os.path.normpath(os.path.join(repo, os.path.expanduser(args[1])))
            args = args[2:]
        if not args or os.path.normpath(repo) != root:
            continue
        if args[0] == 'add':
            paths = [a for a in args[1:] if not a.startswith('-')]
            everything = any(a in ('-A', '--all', '-u', '--update') for a in args[1:]) or paths == ['.']
            found.append(('add', None if everything or not paths else [os.path.join(repo, p) for p in paths]))
        elif args[0] in ('rm', 'mv'):
            # both stage what they remove, and `git mv` what it adds
            paths = [a for a in args[1:] if not a.startswith('-')]
            found.append(('add', [os.path.join(repo, p) for p in paths]))
        elif args[0] == 'commit':
            message, i = None, 1
            while i < len(args):
                a = args[i]
                if a in ('-m', '--message') and i + 1 < len(args):
                    message = args[i + 1] if message is None else message + '\n\n' + args[i + 1]
                    i += 2
                    continue
                if a.startswith('--message='):
                    message = a.split('=', 1)[1]
                elif re.fullmatch(r'-[a-zA-Z]*m', a) and i + 1 < len(args):
                    message = args[i + 1]
                    i += 2
                    continue
                i += 1
            amend = '--amend' in args
            every = any(re.fullmatch(r'-[a-zA-Z]*a[a-zA-Z]*', a) for a in args[1:]) or '--all' in args
            found.append(('commit', message, amend, every))
        elif args[0] == 'rebase' and '--autosquash' in args:
            found.append(('autosquash',))
        elif args[0] == 'tag':
            found += tag_operations(args[1:])
    return found


LISTING = ('-l', '--list', '-v', '--verify', '-n', '--contains', '--no-contains', '--points-at', '--sort',
           '--format', '--merged', '--no-merged')


def tag_operations(args):
    """`git tag`: ('tag', name, revision or None, message or None) for a tag
    made, ('untag', name) for each one deleted, nothing for a listing."""
    if any(a in ('-d', '--delete') for a in args):
        return [('untag', a) for a in args if not a.startswith('-')]
    if any(a.split('=')[0] in LISTING or re.fullmatch(r'-n\d+', a) for a in args):
        return []
    names, message, i = [], None, 0
    while i < len(args):
        a = args[i]
        if (a in ('-m', '--message') or re.fullmatch(r'-[a-zA-Z]*m', a)) and i + 1 < len(args):
            message = args[i + 1] if message is None else message + '\n\n' + args[i + 1]
            i += 2
            continue
        if a in ('-F', '--file', '-u', '--local-user') and i + 1 < len(args):
            i += 2
            continue
        if a.startswith('--message='):
            message = a.split('=', 1)[1]
        elif not a.startswith('-'):
            names.append(a)
        i += 1
    return [('tag', names[0], names[1] if len(names) > 1 else None, message)] if names else []


class Replay:
    """Replays a timeline on the project's tree and cuts it into units.

    public(rel) says whether a file belongs in the history. checkpoints are
    (moment, folder, name): copies of the project whose files the tree takes
    at that moment, the current tree last. markers, a compiled pattern,
    finds in what the agent says the defect it moves to. made, from the
    project's reflog, are the commits git records: (unix time, amend,
    subject, hash); commit_files(hash, leave) gives the files of one of them
    but those in leave; subject_of(revision) the subject of the commit a
    revision names.

    tags are the agent's `git tag`s, in order: ('tag', units closed before
    it, name, subject of the commit it names, message, moment) and ('untag',
    units closed before it, name)."""

    def __init__(self, root, public, checkpoints=(), markers=None, repository=True, made=(),
                 commit_files=None, subject_of=None):
        self.root = root.rstrip('/')
        self.public = public
        # a `git commit` in a folder that is no repository did nothing, even
        # when a pipe hid its failure
        self.repository = repository
        self.made = list(made)
        self.commit_files = commit_files
        self.subject_of = subject_of
        # the files known from git only, until the logs write them
        self.from_git = set()
        # the units of `git commit --fixup` a rebase has yet to squash
        self.fixups = []
        # the subjects of the agent's commits, HEAD last
        self.heads = []
        self.tags = []
        self.ids = set()
        self.tree = Tree(self.root, Tracked())
        self.committed = {}
        self.units = []
        self.current = {}
        self.checkpoints = sorted(checkpoints)
        self.markers = markers
        self.staged = set()
        self.stage_all = False
        self.stats = collections.Counter()
        self.unknown = []
        self.drift = []

    # --- units ------------------------------------------------------------

    def start(self, session, moment, request, marker=None):
        """A unit: a new request (marker None), or the same request going on
        past a defect marker or a commit."""
        previous = self.current.get(session)
        if previous is not None and previous['paths']:
            self.end(previous)
        same = marker is not None and previous is not None
        base = f'{moment}/{session}' + (f'#{marker}' if marker else '')
        # two commits in one second each start a unit at the same moment
        uid, n = base, 1
        while uid in self.ids:
            n += 1
            uid = f'{base}~{n}'
        self.ids.add(uid)
        unit = {'id': uid, 'session': session,
                'start': moment, 'end': moment, 'request': request, 'marker': marker, 'paths': set(),
                'notes': [], 'agent': None, 'unknown': [],
                'request_id': previous['request_id'] if same else f'{moment}/{session}'}
        self.current[session] = unit
        return unit

    def unit_of(self, session, moment):
        return self.current.get(session) or self.start(session, moment, '')

    def end(self, unit, paths=None):
        """Closes a unit: what changed in the files it touched (or in paths)
        since the last unit, if anything did."""
        if unit.get('closed'):
            return
        unit['closed'] = True
        chosen = unit['paths'] if paths is None else paths
        changes = {}
        for p in sorted(chosen):
            if not self.public(p):
                continue
            now = self.tree.files.get(p)
            if now != self.committed.get(p):
                changes[p] = now
        if not changes and unit['agent'] is None:
            return
        stats = {p: stat(self.committed.get(p), content) for p, content in changes.items()}
        for p, content in changes.items():
            self.committed[p] = content
        self.units.append({
            'id': unit['id'], 'session': unit['session'], 'date': unit['end'], 'request': unit['request'],
            'request_id': unit['request_id'],
            'summary': unit['notes'][-1] if unit['notes'] else '', 'marker': unit['marker'],
            'agent': unit['agent'], 'unknown': unit['unknown'], 'changes': changes, 'stat': stats})

    def agent_commit(self, session, moment, message, amend, every, commit=None):
        """A commit the agent made: the unit ends with what it had staged, and
        the work goes on in a new one under the same request. commit is its
        hash, when the reflog has it."""
        unit = self.unit_of(session, moment)
        if every:
            self.stage_all = True
        if commit and self.commit_files:
            # a file the logs never describe, one a tool wrote (a lock file),
            # is as git recorded it, at each commit
            known = (set(self.tree.files) | set(self.committed)) - self.from_git
            unseen = self.commit_files(commit, known)
            for p, content in unseen.items():
                self.tree.files[p] = content
                self.staged.add(p)
            self.tree.files.dirty -= set(unseen)
            self.stats['files taken from the commits git recorded'] += len(set(unseen) - self.from_git)
            self.from_git |= set(unseen)
        unit['agent'] = {'message': message or '', 'amend': amend}
        if unit['agent']['message'].startswith('fixup! '):
            self.fixups.append(unit['agent'])
        subject = (message or '').split('\n')[0]
        if not amend or not self.heads:
            self.heads.append(subject)
        elif subject:
            self.heads[-1] = subject
        unit['end'] = moment
        if self.stage_all:
            paths = {p for p in set(self.tree.files) | set(self.committed)
                     if self.tree.files.get(p) != self.committed.get(p)}
        else:
            paths = set(self.staged)
        self.end(unit, paths)
        # what was changed but not staged waits for the next commit
        left = unit['paths'] - paths
        self.staged, self.stage_all = set(), False
        following = self.start(session, moment, unit['request'], f'after {moment}')
        following['paths'] = set(left)

    # --- the timeline -----------------------------------------------------

    def checkpoint(self, moment, folder, name, files):
        """The copy is the truth: the tree takes it, and what differed is a
        unit of its own. A copy speaks only for the top folders it holds."""
        covered = {p.split('/')[0] for p in files}
        replayed = {p: c for p, c in self.tree.files.items()
                    if self.public(p) and ('/' not in p or p.split('/')[0] in covered)}
        differ = sorted(p for p in set(files) | set(replayed) if files.get(p) != replayed.get(p))
        for p in differ:
            if p in files:
                self.tree.files[p] = files[p]
            else:
                self.tree.files.pop(p, None)
        self.tree.files.dirty.clear()
        self.drift.append((name, moment, len(files), differ))
        if differ:
            unit = {'id': f'{moment}/copy {name}', 'session': 'copy', 'start': moment, 'end': moment,
                    'request': f'the project as saved in {name}', 'marker': None, 'paths': set(differ),
                    'notes': [], 'agent': None, 'unknown': [], 'request_id': f'{moment}/copy {name}'}
            self.end(unit)

    def run(self, items, line, read_copy):
        """items from logs.timeline; line maps a session to its line of work;
        read_copy(folder) gives a copy's public files."""
        marks = list(self.checkpoints)
        for moment, session, kind, payload in items:
            while marks and marks[0][0] <= moment:
                self.close_all()
                when, folder, name = marks.pop(0)
                self.checkpoint(when, folder, name, read_copy(folder))
            session = line.get(session, session)
            if kind == 'request':
                self.start(session, moment, payload)
                self.stats['requests'] += 1
                continue
            unit = self.unit_of(session, moment)
            if kind == 'note':
                unit['notes'].append(payload)
                if self.markers is not None:
                    found = list(dict.fromkeys(self.markers.findall(payload)))
                    if 1 <= len(found) <= 2 and found[-1] != unit['marker']:
                        self.start(session, moment, unit['request'], found[-1])
                continue
            if kind in ('backup', 'seen'):
                # a file as it was: a backup on disk, or what a command printed
                path, source = payload
                rel = self.tree.rel(path)
                if rel is None:
                    continue
                if kind == 'backup':
                    with open(source, encoding='utf-8', errors='surrogateescape', newline='') as f:
                        source = f.read()
                name = 'backups' if kind == 'backup' else 'files printed'
                self.stats[name] += 1
                if rel in self.tree.files and self.tree.files[rel] != source:
                    self.stats[name + ' correcting the replay'] += 1
                self.tree.files[rel] = source
            elif kind == 'edit':
                self.edit(*payload)
            ops = []
            if kind == 'bash':
                command, cwd, failed, reset, ended = payload
                ops = git_operations(command, cwd or self.root, self.root) if self.repository else []
                # a failed command may have done part of its work, but which
                # part is not known; a backup or a copy will say. Its commits
                # count where the reflog shows them made.
                ops = self.made_by(ops, moment, ended, failed)
                if not failed:
                    unknown = run_bash(self.tree, command, cwd or self.root)
                    if unknown:
                        self.stats['shell commands not replayed'] += len(unknown)
                        unit['unknown'] += unknown
                        self.unknown += [(moment, u) for u in unknown]
                    if self.tree.files.dirty:
                        self.stats['shell commands replayed'] += 1
            changed = set(self.tree.files.dirty)
            self.tree.files.dirty.clear()
            self.from_git -= changed
            if changed:
                unit['paths'] |= changed
                unit['end'] = moment
            for op in ops:
                if op[0] == 'add':
                    self.stage(op[1])
                elif op[0] == 'autosquash':
                    for agent in self.fixups:
                        agent['squashed'] = True
                    self.fixups = []
                    self.heads = [h for h in self.heads if not h.startswith('fixup! ')]
                elif op[0] == 'tag':
                    subject = self.named(op[2])
                    if subject is None:
                        self.stats['tags on a commit not found'] += 1
                    else:
                        self.tags.append(('tag', len(self.units), op[1], subject, op[3], moment))
                elif op[0] == 'untag':
                    self.tags.append(('untag', len(self.units), op[1]))
                else:
                    self.stats['agent commits'] += 1
                    self.agent_commit(session, op[4], op[1], op[2], op[3], op[5])
        self.close_all()
        for when, folder, name in marks:
            self.checkpoint(when, folder, name, read_copy(folder))
        return self.units

    def named(self, revision):
        """The subject of the commit a revision names: HEAD and its ancestors
        as the replay made them, anything else as the project's git knows it."""
        m = re.fullmatch(r'HEAD((?:[~^]\d*)*)|@', revision or 'HEAD')
        if m:
            back = sum(int(s[1:] or 1) if s[0] == '~' else 1 for s in re.findall(r'[~^]\d*', m.group(1) or ''))
            return self.heads[-1 - back] if back < len(self.heads) else None
        return self.subject_of(revision) if self.subject_of else None

    def made_by(self, ops, start, ended, failed):
        """The git operations of a command that took effect, each commit with
        the moment and the hash the reflog gives it when it shows it made
        while the command ran, else the command's moment; and with the
        reflog's subject when the command gave no message (`--fixup`, `-F`).
        A failed command keeps those up to the last of its commits the
        reflog shows."""
        low = seconds(start) - 1
        high = seconds(ended) + 1 if ended else low + 600
        done, kept = [], 0
        for op in ops:
            if op[0] != 'commit':
                done.append(op)
                continue
            subject = op[1].split('\n')[0] if op[1] else None
            found = next((i for i, (when, amend, said, _) in enumerate(self.made)
                          if low <= when <= high and amend == op[2] and subject in (None, said)), None)
            if found is None and failed:
                break
            if found is None:
                done.append(op + (start, None))
            else:
                when, _, said, commit = self.made.pop(found)
                message = said if op[1] is None and not op[2] else op[1]
                done.append(('commit', message, op[2], op[3], max(start, utc(when)), commit))
            kept = len(done)
        return done[:kept] if failed else done

    def stage(self, paths):
        """`git add`: everything, or the files under the paths given."""
        if paths is None:
            self.stage_all = True
            return
        for path in paths:
            rel = self.tree.rel(path)
            if rel is not None:
                self.staged |= {k for k in set(self.tree.files) | set(self.committed)
                                if rel == '' or k == rel or k.startswith(rel + '/')}

    def close_all(self):
        for unit in list(self.current.values()):
            if unit['paths']:
                self.end(unit)
        self.current = {}

    def edit(self, tool, args):
        rel = self.tree.rel(args.get('file_path', ''))
        if rel is None:
            return
        if tool == 'Patch':
            self.patch(rel, args)
            return
        if tool == 'Write':
            self.tree.files[rel] = args.get('content', '')
            self.stats['writes'] += 1
            return
        for one in args.get('edits') or [args]:
            text = self.tree.files.get(rel)
            old = one.get('old_string', '')
            if text is None or old not in text:
                # the file as the replay has it is not the one the agent saw
                self.stats['edits that did not apply'] += 1
                return
            new = one.get('new_string', '')
            self.tree.files[rel] = text.replace(old, new) if one.get('replace_all') else text.replace(old, new, 1)
        self.stats['edits'] += 1

    def patch(self, rel, change):
        """A file Codex added, deleted, or updated by a diff (and moved)."""
        kind = change.get('type')
        if kind == 'add':
            self.tree.files[rel] = change.get('content', '')
        elif kind == 'delete':
            self.tree.files.pop(rel, None)
        elif kind == 'update':
            text = self.tree.files.get(rel)
            new = None if text is None else apply_diff(text, change.get('unified_diff', ''))
            if new is None:
                # the file as the replay has it is not the one the agent saw
                self.stats['patches that did not apply'] += 1
                return
            target = self.tree.rel(change['move_path']) if change.get('move_path') else rel
            if target != rel:
                del self.tree.files[rel]
            if target is not None:
                self.tree.files[target] = new
        else:
            return
        self.stats['patches'] += 1


HUNK = re.compile(r'@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@')


def apply_diff(content, diff):
    """The content once the hunks of a unified diff are applied, None if one
    does not apply. A hunk goes where its line numbers say, else where its
    old lines first appear after the hunk before it."""
    lines = content.split('\n')
    end = content.endswith('\n')
    if end:
        lines.pop()
    hunks, last, missing = [], None, set()
    rows = diff.split('\n')
    if rows and rows[-1] == '':
        rows.pop()
    for row in rows:
        m = HUNK.match(row)
        if m:
            hunks.append((int(m.group(1)), [], []))
        elif row.startswith('\\'):
            # "\ No newline at end of file", for the side of the line above
            missing |= {'-': {'old'}, '+': {'new'}}.get(last, {'old', 'new'})
        elif hunks:
            last, text = (row[0], row[1:]) if row else (' ', '')
            if last in ' -':
                hunks[-1][1].append(text)
            if last in ' +':
                hunks[-1][2].append(text)
    if not hunks:
        return None
    out, at = [], 0
    for start, old, new in hunks:
        where = start - 1 if old else start
        if lines[where:where + len(old)] != old or where < at:
            where = next((i for i in range(at, len(lines) - len(old) + 1) if lines[i:i + len(old)] == old), None)
            if where is None or not old:
                return None
        out += lines[at:where] + new
        at = where + len(old)
    out += lines[at:]
    if missing:
        end = 'new' not in missing
    return '\n'.join(out) + ('\n' if end and out else '')
