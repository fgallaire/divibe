"""The command line.

    divibe PROJECT            the report, then the history in PROJECT-divibe
    divibe -n PROJECT         the report alone: a dry run writes nothing
    divibe --verify PROJECT   rebuild a git project's history and compare it with its own

When commits need a message, divibe PROJECT writes them in a file next to
the project, PROJECT-divibe-messages.txt, and stops: the next run reads it.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile

from . import build, codex, logs, opencode
from .project import Project
from .replay import Replay


def retention():
    """How long Claude Code keeps session logs, in days: cleanupPeriodDays,
    30 when it is not set."""
    try:
        with open(os.path.expanduser('~/.claude/settings.json')) as f:
            return int(json.load(f).get('cleanupPeriodDays', 30)), True
    except (OSError, ValueError, TypeError):
        return 30, False


def parse_copy(text):
    """FOLDER@MOMENT, the moment in ISO form, local time when it has no zone."""
    folder, _, when = text.rpartition('@')
    if not folder:
        raise argparse.ArgumentTypeError('a copy is FOLDER@MOMENT')
    stamp = datetime.datetime.fromisoformat(when)
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return (stamp.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            os.path.realpath(folder), os.path.basename(os.path.realpath(folder)))


class Analysis:
    """The logs of a project, replayed."""

    def __init__(self, args):
        self.project = Project(args.project, args.exclude)
        root = self.project.root
        files = logs.session_files(root, args.logs)
        events, ids = logs.load(files)
        items = logs.timeline(events, root, logs.backup_index(args.backups))
        lines = logs.lineages(ids)
        rollouts = codex.session_files(root, args.codex)
        items += codex.timeline(rollouts)
        count, more, more_lines = opencode.timeline(root, args.opencode)
        items += more
        lines.update(more_lines)
        # the agents each give their sessions, merged in time
        self.sources = {'Claude Code': len(files), 'Codex': len(rollouts), 'OpenCode': count}
        self.items = sorted(items, key=lambda i: i[0])
        self.now = self.project.files()
        candidates = {it[3][0] if it[2] == 'backup' else it[3][1].get('file_path', '') for it in self.items
                      if it[2] in ('backup', 'edit')}
        self.project.decide([os.path.relpath(p, root) for p in candidates
                             if p.startswith(root + '/')] + list(self.now))
        moment = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        checkpoints = list(args.copy or []) + [(moment, root, 'the current tree')]
        # a project older than the logs starts from its last commit before them
        self.base = self.project.commit_before(self.items[0][0]) if self.project.git and self.items else None
        if self.base:
            checkpoints.append((self.items[0][0], 'git:' + self.base, 'commit ' + self.base[:7]))
        markers = re.compile(args.markers) if args.markers else None
        self.replay = Replay(root, self.project.public, checkpoints, markers, repository=self.project.git,
                             made=self.project.made, commit_files=self.project.commit_files,
                             subject_of=self.project.subject_of)
        self.units = self.replay.run(self.items, lines, self.read_copy)
        # the project as the agent found it is a commit of its own, never
        # folded into the agent's first one
        self.given = {u['id']: f'The project before the agent, as in commit {self.base[:7]}' for u in self.units
                      if self.base and u['session'] == 'copy' and u['request'].endswith('commit ' + self.base[:7])}

    def messages(self):
        """The messages of the units: those written in the file, else given."""
        return dict(self.given, **read_messages(messages_path(self.project.root), self.units))

    def read_copy(self, folder):
        if folder == self.project.root:
            return self.now
        if folder.startswith('git:'):
            return self.project.commit_files(folder[4:])
        return self.project.files(folder)


def common(parser):
    parser.add_argument('project', help='the project folder')
    parser.add_argument('--exclude', action='append', default=[], help='a path pattern to leave out, as in .gitignore')
    parser.add_argument('--copy', action='append', type=parse_copy, help='a copy of the project: FOLDER@MOMENT')
    parser.add_argument('--markers', help='a pattern whose first group names a defect in what the agent says')
    parser.add_argument('--logs', default=logs.PROJECTS, help=argparse.SUPPRESS)
    parser.add_argument('--backups', default=logs.BACKUPS, help=argparse.SUPPRESS)
    parser.add_argument('--codex', default=codex.HOME, help=argparse.SUPPRESS)
    parser.add_argument('--opencode', default=opencode.DATABASE, help=argparse.SUPPRESS)


# --- the report ------------------------------------------------------------

def report(a, recorded):
    """What the logs hold, the quality to expect, and the commits to come."""
    r = a.replay
    stats = r.stats
    root = a.project.root
    # the moments of the work on this project, not of the whole sessions
    moments = sorted(it[0] for it in a.items if (it[2] == 'edit' and it[3][1].get('file_path', '').startswith(root + '/'))
                     or (it[2] == 'bash' and (it[3][1] + '/').startswith(root + '/'))
                     or it[2] == 'backup')
    requests = len({u['request_id'] for u in a.units if u['session'] != 'copy'})
    days, set_here = retention()
    referenced = sum(1 for it in a.items if it[2] == 'backup')
    final = r.drift[-1]
    differ = final[3]
    copies = r.drift[:-1]
    agent = sum(1 for u in a.units if u.get('agent'))
    unknown = stats['shell commands not replayed']
    out = []
    say = out.append
    say(f'Divibe: {a.project.root}')
    say('')
    say('Sources')
    span = f', {moments[0][:16].replace("T", " ")} to {moments[-1][:16].replace("T", " ")} UTC' if moments else ''
    found = ', '.join(f'{name} {n}' for name, n in a.sources.items() if n) or 'none'
    say(f'  session logs         {found}, naming the project{span}')
    if days < 365 and a.sources['Claude Code']:
        say(f'                       Claude Code deletes logs after {days} days'
            f'{"" if set_here else " (the default)"}: set cleanupPeriodDays in ~/.claude/settings.json to keep them')
    say(f'  file backups         {referenced} found for the project\'s files')
    if a.base:
        say(f'  the project before   commit {a.base[:7]}, the last one before the logs')
    say(f'  copies you gave      {len(copies) - bool(a.base) or "none"}')
    say(f'  the project now      {len(a.now)} files ({"what git tracks and does not ignore" if a.project.git else "outside git: usual folders and .gitignore left out"})')
    say('')
    say('What the logs describe')
    say(f'  requests             {requests} that changed the project')
    say(f'  Write                {stats["writes"]:<5} exact')
    say(f'  Edit                 {stats["edits"]:<5} exact, {stats["edits that did not apply"]} found the file different')
    if a.sources['Codex']:
        say(f'  Codex patch          {stats["patches"]:<5} exact, {stats["patches that did not apply"]} found the file '
            'different')
    say(f'  shell, replayed      {stats["shell commands replayed"]:<5} emulated (sed -i, cat >, cp, mv, rm, a Python heredoc...)')
    say(f'  shell, not replayed  {unknown:<5} a build, a package manager, a script, git rewriting files...')
    for when, command in r.unknown[:5]:
        say(f'                         {when[:16]}  {command[:70]}')
    say(f'  backups that corrected the replay  {stats["backups correcting the replay"]}')
    if stats['files printed']:
        say(f'  files Codex printed whole  {stats["files printed"]}, {stats["files printed correcting the replay"]} '
            'of them correcting the replay')
    say(f'  commits by the agent {agent:<5} kept as they were: their cut, their message, their moment')
    placed = sum(1 for t in r.tags if t[0] == 'tag')
    if placed or stats['tags on a commit not found']:
        say(f'  tags by the agent    {placed:<5} placed on the commits they named'
            + (f', {stats["tags on a commit not found"]} on a commit git no longer has'
               if stats['tags on a commit not found'] else ''))
    taken = stats['files taken from the commits git recorded']
    if taken:
        say(f'  files no log describes  {taken} written by a tool (a lock file...) or older than the logs, '
            'taken from the commits git recorded')
    say('')
    say('The replay against the project as it stands')
    say(f'  identical            {len(a.now) - sum(1 for p in differ if p in a.now)} files')
    say(f'  different            {len(differ)} files' + (': ' + ', '.join(differ[:8]) + ('...' if len(differ) > 8 else '') if differ else ''))
    for name, when, count, d in copies:
        say(f'  copy {name} ({when[:16]}): {len(d)} of {count} files taken from the copy')
    say('')
    ratio = len(differ) / max(1, len(a.now))
    if not differ and not stats['edits that did not apply'] and not stats['patches that did not apply'] \
            and not stats['backups correcting the replay'] and not stats['files printed correcting the replay']:
        grade, why = 'exact', ('the replay never had to be corrected and ends on the project as it stands'
                               + (': the commands not replayed changed no file it tracks.' if unknown else '.'))
    elif ratio <= 0.05:
        grade, why = 'close', ('a few files are not where the replay leaves them; the last commit takes them as '
                               'they are, so the commits before it may miss part of their changes.')
    else:
        grade, why = 'partial', ('much of the work is not in the logs (or the logs are gone): the commits will '
                                 'hold what the logs show, and the last one a large catch-up. Copies of the '
                                 'project from the past (--copy FOLDER@MOMENT) make it better.')
    say(f'Expected quality: {grade}')
    say(f'  {why}')
    effective = build.effective_messages(a.units, recorded)
    need = sum(1 for m in effective.values() if m is None)
    commits = build.commits_of(a.units, recorded, draft=True)[0]
    say('')
    say(f'History: {len(commits)} commits, {sum(1 for c in commits if c[4])} of them made by the agent'
        + (f'; {need} need a message' if need else '') + '.')
    return '\n'.join(out)


# --- the messages to write -------------------------------------------------

HEADER = '''# The commits of {root} that need a message.
# Write each message after its key: the request it comes from is filled in.
# Then run divibe again, and it writes the history. A message "+" puts the
# work into the next commit, "-" leaves it out.
'''


def messages_path(root):
    """The file of the messages to write, next to the project."""
    return root.rstrip('/') + '-divibe-messages.txt'


def keys(units):
    """A short key for each unit, the same from one run to the next."""
    for size in range(6, 33, 2):
        found = {u['id']: hashlib.md5(u['id'].encode()).hexdigest()[:size] for u in units}
        if len(set(found.values())) == len(found):
            break
    return found


def read_messages(path, units):
    """The messages written in the file, by unit."""
    if not os.path.exists(path):
        return {}
    ids = {key: uid for uid, key in keys(units).items()}
    found = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            m = re.match(r'([0-9a-f]{6,32})[ \t]+(\S.*?)\s*$', line)
            if m and m.group(1) in ids:
                found[ids[m.group(1)]] = m.group(2)
    return found


def write_messages(path, root, units, recorded, effective):
    """The file: the messages already written, and for each commit that
    needs one the first line of its request, with what the commit holds."""
    key = keys(units)
    rows = [HEADER.format(root=root)]
    for u in units:
        if u['id'] not in recorded and effective[u['id']] is not None:
            continue
        asked = ' '.join(u['request'].split())
        message = recorded.get(u['id']) or u['request'].strip().split('\n')[0][:72] or 'Work'
        rows.append(f'\n{key[u["id"]]}  {message}\n')
        rows.append(f'#  {u["date"][:16].replace("T", " ")} UTC, asked: {asked[:90]}\n')
        if u['summary'].strip():
            rows.append(f'#  the agent said: {" ".join(u["summary"].split())[:100]}\n')
        rows.append('#  files: ' + ', '.join(f'{p} +{s[0]}/-{s[1]}' for p, s in u['stat'].items())[:110] + '\n')
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(rows)


# --- divibe PROJECT ----------------------------------------------------------

def run(args):
    """The report, then the history, unless it is a dry run or commits need
    a message; or, with --verify, the comparison."""
    a = Analysis(args)
    if args.verify:
        return verify(a)
    path = messages_path(a.project.root)
    recorded = a.messages()
    print(report(a, recorded))
    if args.dry_run:
        return 0
    effective = build.effective_messages(a.units, recorded)
    need = sum(1 for m in effective.values() if m is None)
    if need:
        write_messages(path, a.project.root, a.units, recorded, effective)
        print(f'\n{need} commits need a message: they are in {path}.\n'
              'Correct them as you like, then run divibe again.')
        return 0
    out = os.path.abspath(args.out) if args.out else a.project.root + '-divibe'
    if os.path.exists(out) and os.listdir(out):
        sys.exit(f'{out} already exists: remove it, or give another folder with --out')
    commits, _ = build.commits_of(a.units, recorded)
    notes = {c[0]: u['request'] for c in commits for u in a.units if u['id'] == c[0]} if args.notes else None
    tags = build.tags_of(a.units, commits, a.replay.tags)
    made = build.write(out, commits, build.plan_dates(commits), build.identity(a.project.root), notes, tags=tags)
    last = build.tree_files(out)
    differ = sorted(p for p in set(last) | set(a.now) if last.get(p) != a.now.get(p))
    print(f'\n{made} commits and {len(build.tags(out))} tags in {out}; '
          + ('the last commit is the project as it stands' if not differ else
             f'the last commit differs from the project in {len(differ)} files: {", ".join(differ[:8])}'))
    return 0


# --- divibe --verify PROJECT -------------------------------------------------


def compare_histories(first, second):
    """Prints how two histories differ, commit by commit; 0 if they hold the
    same commits, in message and tree."""
    one, two = build.history(first), build.history(second)
    same, apart = 0, 0.0
    for i, (a, b) in enumerate(zip(one, two)):
        apart = max(apart, abs((build.moment(a[2]) - build.moment(b[2])).total_seconds()))
        if a[0] == b[0] and a[1] == b[1]:
            same += 1
            continue
        what = 'message' if a[1] == b[1] else 'tree' if a[0] == b[0] else 'message and tree'
        print(f'commit {i + 1}: {what} differ\n    {a[0][:70]}\n    {b[0][:70]}')
    print(f'{same} of {max(len(one), len(two))} commits identical in message and tree '
          f'({len(one)} and {len(two)} commits), dates at most {apart:.0f} s apart')
    first_tags, second_tags = build.tags(first), build.tags(second)
    names = sorted(set(first_tags) | set(second_tags))
    for name in names:
        if first_tags.get(name) != second_tags.get(name):
            where = [f'commit {t[name]}' if name in t else 'none' for t in (first_tags, second_tags)]
            print(f'tag {name}: {where[0]} and {where[1]}')
    alike = sum(1 for n in names if first_tags.get(n) == second_tags.get(n))
    if names:
        print(f'{alike} of {len(names)} tags on the same commit')
    return 0 if same == len(one) == len(two) and alike == len(names) else 1


def verify(a):
    """Rebuilds a project's history from the logs, in a temporary folder, and
    compares it with the project's own git history."""
    if not a.project.git:
        sys.exit(f'{a.project.root} has no git history to compare with')
    messages = a.messages()
    missing = build.commits_of(a.units, messages)[1]
    commits, _ = build.commits_of(a.units, messages, draft=True)
    folder = tempfile.mkdtemp(prefix='divibe-')
    out = os.path.join(folder, 'rebuilt')
    build.write(out, commits, build.plan_dates(commits), build.identity(a.project.root),
                tags=build.tags_of(a.units, commits, a.replay.tags))
    status = compare_histories(a.project.root, out)
    if missing:
        print(f'{len(missing)} units with no message became draft commits: work not committed yet, '
              'or committed outside the logs')
    print(f'rebuilt history kept in {out}')
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='divibe',
        description="A true git history for a project written with an AI agent, rebuilt from the agent's own "
                    "records: what they hold and the quality to expect, then the history in PROJECT-divibe, next "
                    "to the project.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('-n', '--dry-run', action='store_true', help='say what the history would be, and write nothing')
    mode.add_argument('--verify', action='store_true',
                      help='for a project already in git: rebuild its history in a temporary folder and compare it '
                           'with its own, commit by commit')
    common(parser)
    parser.add_argument('--out', help='the new repository (default: PROJECT-divibe, next to the project)')
    parser.add_argument('--notes', action='store_true', help="attach each commit's request as a git note")
    return run(parser.parse_args(argv)) or 0
