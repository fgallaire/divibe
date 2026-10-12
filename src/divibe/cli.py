"""The command line: survey, units, messages, build, compare, verify."""
import argparse
import datetime
import json
import os
import re
import sys
import tempfile

from . import build, logs
from .project import Project
from .replay import Replay

CACHE = os.path.expanduser('~/.cache/divibe')


def state_dir(root, given=None):
    folder = given or os.path.join(CACHE, re.sub(r'[^A-Za-z0-9]', '-', root))
    os.makedirs(folder, exist_ok=True)
    return folder


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
        self.files = logs.session_files(root, args.logs)
        events, ids = logs.load(self.files)
        self.events = events
        self.items = logs.timeline(events, root, logs.backup_index(args.backups))
        self.now = self.project.files()
        candidates = {it[3][0] if it[2] == 'backup' else it[3][1].get('file_path', '') for it in self.items
                      if it[2] in ('backup', 'edit')}
        self.project.decide([os.path.relpath(p, root) for p in candidates
                             if p.startswith(root + '/')] + list(self.now))
        moment = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        checkpoints = list(args.copy or []) + [(moment, root, 'the current tree')]
        markers = re.compile(args.markers) if args.markers else None
        self.replay = Replay(root, self.project.public, checkpoints, markers, repository=self.project.git,
                             made=self.project.made)
        self.units = self.replay.run(self.items, logs.lineages(ids),
                                     lambda folder: self.now if folder == root else self.project.files(folder))


def common(parser):
    parser.add_argument('project', help='the project folder')
    parser.add_argument('--exclude', action='append', default=[], help='a path pattern to leave out, as in .gitignore')
    parser.add_argument('--copy', action='append', type=parse_copy, help='a copy of the project: FOLDER@MOMENT')
    parser.add_argument('--markers', help='a pattern whose first group names a defect in what the agent says')
    parser.add_argument('--logs', default=logs.PROJECTS, help=argparse.SUPPRESS)
    parser.add_argument('--backups', default=logs.BACKUPS, help=argparse.SUPPRESS)
    parser.add_argument('--state', help='where units and messages are kept (default under ~/.cache/divibe)')


# --- survey ----------------------------------------------------------------

def survey(args):
    a = Analysis(args)
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
    say(f'Divibe survey of {a.project.root}')
    say('')
    say('Sources')
    span = f', {moments[0][:16].replace("T", " ")} to {moments[-1][:16].replace("T", " ")} UTC' if moments else ''
    say(f'  session logs         {len(a.files)} naming the project{span}')
    if days < 365:
        say(f'                       Claude Code deletes logs after {days} days'
            f'{"" if set_here else " (the default)"}: set cleanupPeriodDays in ~/.claude/settings.json to keep them')
    say(f'  file backups         {referenced} found for the project\'s files')
    say(f'  copies you gave      {len(copies) or "none"}')
    say(f'  the project now      {len(a.now)} files ({"what git tracks and does not ignore" if a.project.git else "outside git: usual folders and .gitignore left out"})')
    say('')
    say('What the logs describe')
    say(f'  requests             {requests} that changed the project')
    say(f'  Write                {stats["writes"]:<5} exact')
    say(f'  Edit                 {stats["edits"]:<5} exact, {stats["edits that did not apply"]} found the file different')
    say(f'  shell, replayed      {stats["shell commands replayed"]:<5} emulated (sed -i, cat >, cp, mv, rm, a Python heredoc...)')
    say(f'  shell, not replayed  {unknown:<5} a build, a package manager, a script, git rewriting files...')
    for when, command in r.unknown[:5]:
        say(f'                         {when[:16]}  {command[:70]}')
    say(f'  backups that corrected the replay  {stats["backups correcting the replay"]}')
    say(f'  commits by the agent {agent:<5} kept as they were: their cut, their message, their moment')
    say('')
    say('The replay against the project as it stands')
    say(f'  identical            {len(a.now) - sum(1 for p in differ if p in a.now)} files')
    say(f'  different            {len(differ)} files' + (': ' + ', '.join(differ[:8]) + ('...' if len(differ) > 8 else '') if differ else ''))
    for name, when, count, d in copies:
        say(f'  copy {name} ({when[:16]}): {len(d)} of {count} files taken from the copy')
    say('')
    ratio = len(differ) / max(1, len(a.now))
    if not differ and not stats['edits that did not apply'] and not stats['backups correcting the replay']:
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
    need = sum(1 for u in a.units if not u.get('agent'))
    say('')
    say(f'Units: {len(a.units)}, {agent} with the agent\'s own message, {need} needing one '
        f'(divibe units, then divibe messages).')
    print('\n'.join(out))


# --- units and messages ----------------------------------------------------

def units(args):
    a = Analysis(args)
    folder = state_dir(a.project.root, args.state)
    messages = load_messages(folder)
    with open(os.path.join(folder, 'units.jsonl'), 'w', encoding='utf-8') as f:
        for u in a.units:
            row = {k: v for k, v in u.items() if k != 'changes'}
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    shown = 0
    effective = build.effective_messages(a.units, messages)
    for rank, u in enumerate(a.units):
        message = effective[u['id']]
        if args.todo and message is not None:
            continue
        files = ' '.join(f'{p} +{s[0]}/-{s[1]}' for p, s in u['stat'].items())
        print(f"[{rank}] {u['date'][:16]} {message!r}")
        print(f"    files: {files[:args.width]}")
        print(f"    asked: {' '.join(u['request'].split())[:args.width]}")
        print(f"    said:  {' '.join(u['summary'].split())[:args.width]}")
        shown += 1
    print(f'{len(a.units)} units in {folder}' + (f', {shown} without a message' if args.todo else ''))


def load_messages(folder):
    path = os.path.join(folder, 'messages.json')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def messages(args):
    folder = state_dir(os.path.realpath(args.project), args.state)
    with open(os.path.join(folder, 'units.jsonl'), encoding='utf-8') as f:
        ids = [json.loads(line)['id'] for line in f]
    kept = load_messages(folder)
    added = 0
    with open(args.file, encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            rank, message = line.rstrip('\n').split('\t', 1)
            kept[ids[int(rank)]] = message.strip()
            added += 1
    with open(os.path.join(folder, 'messages.json'), 'w', encoding='utf-8') as f:
        json.dump(kept, f, ensure_ascii=False, indent=1)
    print(f'{added} messages recorded, {sum(1 for i in ids if i in kept)} of {len(ids)} units have one')


# --- build and compare -----------------------------------------------------

def build_command(args):
    a = Analysis(args)
    folder = state_dir(a.project.root, args.state)
    commits, missing = build.commits_of(a.units, load_messages(folder), draft=args.draft)
    if missing:
        sys.exit(f'{len(missing)} units have no message: divibe units --todo, then divibe messages '
                 '(or --draft to take the first line of each request)')
    dates = build.plan_dates(commits)
    notes = {c[0]: u['request'] for c in commits for u in a.units if u['id'] == c[0]} if args.notes else None
    made = build.write(args.out, commits, dates, build.identity(a.project.root), notes)
    last = build.tree_files(args.out)
    differ = sorted(p for p in set(last) | set(a.now) if last.get(p) != a.now.get(p))
    print(f'{made} commits in {args.out}; the last one is the project as it stands'
          if not differ else f'{made} commits in {args.out}; the last one differs from the project in '
                             f'{len(differ)} files: {", ".join(differ[:8])}')


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
    return 0 if same == len(one) == len(two) else 1


def compare(args):
    return compare_histories(args.first, args.second)


def verify(args):
    """Rebuilds a project's history from the logs, in a temporary folder, and
    compares it with the project's own git history."""
    a = Analysis(args)
    if not a.project.git:
        sys.exit(f'{a.project.root} has no git history to compare with')
    messages = load_messages(state_dir(a.project.root, args.state))
    missing = build.commits_of(a.units, messages)[1]
    commits, _ = build.commits_of(a.units, messages, draft=True)
    folder = tempfile.mkdtemp(prefix='divibe-')
    out = os.path.join(folder, 'rebuilt')
    build.write(out, commits, build.plan_dates(commits), build.identity(a.project.root))
    status = compare_histories(a.project.root, out)
    if missing:
        print(f'{len(missing)} units with no message became draft commits: work not committed yet, '
              'or committed outside the logs')
    print(f'rebuilt history kept in {out}')
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='divibe',
        description="A true git history for a project written with an AI agent, rebuilt from the agent's own records.")
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('survey', help='what the logs hold, and the quality to expect')
    common(p)
    p.set_defaults(run=survey)
    p = sub.add_parser('units', help='the units of work, to write their messages')
    common(p)
    p.add_argument('--todo', action='store_true', help='only the units with no message')
    p.add_argument('--width', type=int, default=160)
    p.set_defaults(run=units)
    p = sub.add_parser('messages', help='record messages: a file of RANK<TAB>MESSAGE lines')
    p.add_argument('project')
    p.add_argument('file')
    p.add_argument('--state')
    p.set_defaults(run=messages)
    p = sub.add_parser('build', help='write the history into a new repository')
    common(p)
    p.add_argument('--out', required=True, help='the new repository, a folder that does not exist yet')
    p.add_argument('--draft', action='store_true', help='a unit with no message takes its request\'s first line')
    p.add_argument('--notes', action='store_true', help='attach each commit\'s request as a git note')
    p.set_defaults(run=build_command)
    p = sub.add_parser('compare', help='compare two histories, commit by commit')
    p.add_argument('first')
    p.add_argument('second')
    p.set_defaults(run=compare)
    p = sub.add_parser('verify', help='rebuild a git project\'s history from the logs and compare it with its own')
    common(p)
    p.set_defaults(run=verify)
    args = parser.parse_args(argv)
    return args.run(args) or 0
