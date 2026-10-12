"""From units to commits, and from commits to a repository.

A unit's message comes from the messages file, or, for a commit the agent
made, from the agent. A message `+` folds the unit into the next one that
has a message; `-` drops a unit a later one takes back. An amend the agent
made folds into the commit before it, and a `--fixup` commit a rebase with
`--autosquash` squashed folds into the commit it names.

A commit is dated when its work ended. A commit the agent made keeps the
moment it was made. The others of one request are spread out after their
work, minutes apart as a person commits them one after the other, and
before the first commit of the next request; no two commits share a second,
unless the agent made both in it.

A tag the agent made goes on the commit it named. A version file at the
root (pyproject.toml, package.json, Cargo.toml) tags v<version> the commit
where that version first appears, unless the agent placed that tag itself.
"""
import datetime
import hashlib
import json
import os
import re
import subprocess

SECOND = datetime.timedelta(seconds=1)
VERSION_FILES = {'pyproject.toml': r'project|tool\.poetry', 'package.json': None, 'Cargo.toml': r'package'}


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


def settle(commits):
    """Drops from each commit the changes that leave a file as it was: an
    amend that took a change back, a file read again from git."""
    state = {}
    for commit in commits:
        changes = commit[5]
        for path in [p for p, content in changes.items() if state.get(p) == content]:
            del changes[path]
        state.update(changes)


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
                settle(commits)
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


def version_of(path, content):
    """The version a version file gives, None if none: a package.json marked
    private is never released, and 0.0.0 is no version."""
    if content is None:
        return None
    if path == 'package.json':
        try:
            data = json.loads(content)
        except ValueError:
            return None
        version = data.get('version') if isinstance(data, dict) and not data.get('private') else None
    else:
        section = re.search(r'^\[(?:%s)\][ \t]*$(.*?)(?=^\[|\Z)' % VERSION_FILES[path], content, re.M | re.S)
        found = section and re.search(r'^version\s*=\s*["\']([^"\']+)["\']', section.group(1), re.M)
        version = found.group(1) if found else None
    return version if isinstance(version, str) and version != '0.0.0' else None


def tags_of(units, commits, events):
    """The tags of the history, by name: (commit key, message, moment). The
    agent's own, from the replay's events, on the last commit made before
    the tag that has the subject named; then those of the version files."""
    position = {u['id']: i for i, u in enumerate(units)}
    tags = {}
    for event in events:
        if event[0] == 'untag':
            tags.pop(event[2], None)
            continue
        _, before, name, subject, message, when = event
        found = [c for c in commits if position.get(c[0], len(units)) < before and c[1].split('\n')[0] == subject]
        if found:
            tags[name] = (found[-1][0], message, when)
    current = None
    for key, _, _, _, _, changes in commits:
        for path in VERSION_FILES:
            if path in changes:
                version = version_of(path, changes[path])
                if version and version != current:
                    current = version
                    tags.setdefault('v' + version, (key, None, None))
    return tags


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
            # the agent may make two commits in one second; no one else does
            if floor is not None and (times[i] < floor or times[i] == floor and not group[i][4]):
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


def write(repo, commits, dates, author, notes=None, timezone=None, tags=None):
    """Writes the commits into a new repository. notes, by key, are attached
    as git notes; tags are those of tags_of. Returns how many commits were
    made."""
    os.makedirs(repo, exist_ok=True)
    if os.listdir(repo):
        raise SystemExit(f'{repo} is not empty')
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', author[0])
    git(repo, 'config', 'user.email', author[1])
    made, heads = 0, {}
    for key, message, _, _, _, changes in commits:
        if made:
            # a commit that changes nothing is the one before it
            heads[key] = git(repo, 'rev-parse', 'HEAD').strip()
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
        heads[key] = git(repo, 'rev-parse', 'HEAD').strip()
    for name, (key, message, when) in sorted((tags or {}).items()):
        if key not in heads:
            continue
        if message is None:
            git(repo, 'tag', name, heads[key])
            continue
        when = moment(when).astimezone(timezone).isoformat(timespec='seconds') if when else None
        env = dict(os.environ, GIT_COMMITTER_DATE=when) if when else None
        git(repo, 'tag', '-a', name, '-F', '-', heads[key], env=env, input=message)
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


def tags(repo):
    """The tags of a repository, by name: the rank of the commit each one is
    on, the first commit being 1 (0 for one off the history)."""
    rank = {h: i + 1 for i, h in enumerate(git(repo, 'rev-list', '--reverse', 'HEAD').split())}
    out = git(repo, 'for-each-ref', 'refs/tags', '--format=%(refname:short)%00%(objectname)%00%(*objectname)')
    found = {}
    for line in out.splitlines():
        name, target, peeled = line.split('\0')
        found[name] = rank.get(peeled or target, 0)
    return found
