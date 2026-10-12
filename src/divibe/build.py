"""From units to commits, and from commits to a repository.

A unit's message comes from the messages file, or, for a commit the agent
made, from the agent. A message `+` folds the unit into the next one that
has a message; `-` drops a unit a later one takes back. An amend the agent
made folds into the commit before it, and a `--fixup` commit a rebase with
`--autosquash` squashed folds into the commit it names.

A commit is dated when its work ended. A commit the agent made keeps the
moment it was made. The others of one request are spread out after their
work, minutes apart as a person commits them one after the other, and
before the first commit of the next request; no two commits share a second.
"""
import datetime
import hashlib
import os
import subprocess

SECOND = datetime.timedelta(seconds=1)


def moment(text):
    return datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))


def message_of(unit, messages):
    """The message recorded for a unit, else the agent's own; '' for an amend
    that keeps the message it amends; None where one is needed."""
    if unit['id'] in messages:
        return messages[unit['id']]
    agent = unit.get('agent')
    if agent and agent.get('message'):
        return agent['message']
    if agent and agent.get('amend'):
        return ''
    return None


def effective_messages(units, messages):
    """Each unit's message as the build takes it, None where one is needed.

    When the agent committed, its commits are the history: work cut off
    before one of them (a new request, a session that ended) goes into it."""
    found, seen = {}, False
    for unit in reversed(units):
        message = message_of(unit, messages)
        found[unit['id']] = '+' if message is None and seen else message
        seen = seen or bool(unit.get('agent'))
    return found


def commits_of(units, messages, draft=False):
    """The commits the units make, in order: (key, message, natural moment,
    group, fixed, changes). Units with no message stop it, unless draft,
    which takes the first line of the request."""
    commits, pending, missing = [], {}, []
    effective = effective_messages(units, messages)
    for unit in units:
        message = effective[unit['id']]
        if unit.get('agent') and unit['agent'].get('amend') and commits and message in (None, ''):
            message = commits[-1][1]
        if message is None:
            if not draft:
                missing.append(unit['id'])
                continue
            line = (unit['request'].strip().split('\n') or [''])[0]
            message = 'Draft: ' + (line[:70] or unit['id'])
        if message == '-':
            continue
        pending.update(unit['changes'])
        if message == '+':
            continue
        agent = unit.get('agent') or {}
        if agent.get('squashed'):
            # `git rebase --autosquash` folded it into the commit it names
            named = agent['message'][len('fixup! '):].split('\n')[0]
            index = next((i for i in range(len(commits) - 1, -1, -1) if commits[i][1].split('\n')[0] == named),
                         None)
            if index is not None:
                later = set().union(*(c[5] for c in commits[index + 1:]))
                for path, content in pending.items():
                    # a file a later commit changed too takes the fixup's
                    # state from the last one on
                    commits[-1 if path in later else index][5][path] = content
                pending = {}
                continue
        if unit.get('agent') and unit['agent'].get('amend') and commits:
            # an amended commit keeps its author's date, as git keeps it
            key, _, when, group, fixed, changes = commits.pop()
            changes.update(pending)
            commits.append((key, message, when, group, True, changes))
        else:
            commits.append((unit['id'], message, moment(unit['date']), unit['request_id'],
                            bool(unit.get('agent')), dict(pending)))
        pending = {}
    if pending and commits:
        commits[-1][5].update(pending)
    return commits, missing


def plan_dates(commits):
    """The moment of each commit, by key."""
    groups = []
    for commit in commits:
        if groups and groups[-1][-1][3] == commit[3]:
            groups[-1].append(commit)
        else:
            groups.append([commit])
    dates, last = {}, None
    for index, group in enumerate(groups):
        times = []
        for position, (key, _, natural, _, fixed, _) in enumerate(group):
            floor = times[-1] if times else last
            if fixed or floor is None:
                times.append(natural)
                continue
            spread = 90 + int(hashlib.md5(key.encode()).hexdigest(), 16) % 240
            gap = datetime.timedelta(seconds=spread if times else 60)
            times.append(max(natural, floor + gap))
        if index + 1 < len(groups) and len(times) > 1:
            limit = groups[index + 1][0][2] - 20 * SECOND
            if times[-1] > limit > times[0]:
                step = (limit - times[0]) / (len(times) - 1)
                times = [t if group[i][4] else max(times[0] + step * i, group[i][2])
                         for i, t in enumerate(times)]
        for i in range(len(times)):
            floor = times[i - 1] if i else last
            if floor is not None and times[i] <= floor:
                times[i] = floor + SECOND
        for commit, when in zip(group, times):
            dates[commit[0]] = when
        last = times[-1]
    return dates


def git(repo, *args, env=None, input=None):
    return subprocess.run(['git', *args], cwd=repo, env=env, input=input, capture_output=True, text=True,
                          check=True).stdout


def identity(project_root):
    """The author the project's own git would use."""
    def ask(key):
        return subprocess.run(['git', 'config', key], cwd=project_root, capture_output=True, text=True,
                              check=False).stdout.strip()
    return ask('user.name') or 'Divibe', ask('user.email') or 'divibe@localhost'


def write(repo, commits, dates, author, notes=None, timezone=None):
    """Writes the commits into a new repository. notes, by key, are attached
    as git notes. Returns how many commits were made."""
    os.makedirs(repo, exist_ok=True)
    if os.listdir(repo):
        raise SystemExit(f'{repo} is not empty')
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', author[0])
    git(repo, 'config', 'user.email', author[1])
    made = 0
    for key, message, _, _, _, changes in commits:
        for path, content in changes.items():
            target = os.path.join(repo, path)
            if content is None:
                if os.path.exists(target):
                    os.remove(target)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'w', encoding='utf-8', errors='surrogateescape', newline='') as f:
                f.write(content)
        git(repo, 'add', '-A')
        if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=repo).returncode == 0:
            # what was added and taken back leaves nothing to commit
            continue
        when = dates[key].astimezone(timezone).isoformat(timespec='seconds')
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        git(repo, 'commit', '-q', '-F', '-', env=env, input=message)
        if notes and notes.get(key):
            git(repo, 'notes', 'add', '-F', '-', 'HEAD', env=env, input=notes[key])
        made += 1
    return made


def tree_files(repo):
    """The files of the last commit of a repository, with their content."""
    names = git(repo, '-c', 'core.quotepath=false', 'ls-tree', '-r', '-z', '--name-only', 'HEAD').split('\0')
    found = {}
    for name in filter(None, names):
        found[name] = subprocess.run(['git', 'show', f'HEAD:{name}'], cwd=repo, capture_output=True,
                                     check=True).stdout.decode('utf-8', 'surrogateescape')
    return found


def history(repo):
    """The commits of a repository, oldest first: (message, tree hash, date)."""
    out = git(repo, 'log', '--reverse', '--format=%T%x00%aI%x00%B%x1e')
    rows = []
    for record in filter(None, (r.strip('\n') for r in out.split('\x1e'))):
        tree, date, message = record.split('\0', 2)
        rows.append((message.strip(), tree, date))
    return rows
