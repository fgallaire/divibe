"""Claude Code's records of a project: the session logs, and the backups it
keeps of a file before a turn changes it.

A session log is one JSON event per line. The events this reads:

- a user event whose content is text: a request;
- an assistant event: its text blocks are what the agent said, its tool_use
  blocks the calls it made (Edit, Write, MultiEdit, Bash), each with the
  directory the shell was in (the event's `cwd`);
- a user event holding tool_result blocks: whether a call failed, and for
  Bash, whether the shell was put back in its directory;
- a file-history-snapshot: the backups taken for one prompt, by path.
"""
import glob
import json
import os
import re

PROJECTS = os.path.expanduser('~/.claude/projects')
BACKUPS = os.path.expanduser('~/.claude/file-history')

# text a user event carries that no one typed
NOISE = ('<', '##', 'This session is being continued', '[Request interrupted', 'Caveat:')
RESET = re.compile(r'Shell cwd was reset to (\S+)')


def session_files(target, projects=PROJECTS):
    """Every session log that names the target directory: those of the
    project itself, even one that only ran commands there, and those of
    sessions run from elsewhere that changed files in it."""
    inside, itself = target.rstrip('/') + '/', json.dumps(target.rstrip('/'))
    found = []
    for path in sorted(glob.glob(os.path.join(projects, '**', '*.jsonl'), recursive=True)):
        with open(path, encoding='utf-8', errors='replace') as f:
            if any(inside in line or itself in line for line in f):
                found.append(path)
    return found


def first_moment(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = re.search(r'"timestamp":"([^"]+)"', line)
            if m:
                return m.group(1)
    return '~'


def load(files):
    """Every event once, with the session that made it, and for each session
    the ids it holds. A session that continues another after a compaction
    copies events of it: those stay with the one that made them, read
    first."""
    events, ids, seen = [], {}, set()
    for path in sorted(files, key=lambda p: (first_moment(p), p)):
        session = os.path.splitext(os.path.basename(path))[0]
        ids[session] = set()
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                snapshot = event.get('type') == 'file-history-snapshot'
                uid = event.get('uuid') or (event.get('messageId') if snapshot else None)
                if uid:
                    ids[session].add(uid)
                if not event.get('timestamp') and not snapshot:
                    continue
                # A snapshot carries the id of the prompt it was taken for, and
                # is written again under that id as the turn backs files up: it
                # is one event per text. Anything else is one event per id.
                key = ('snapshot', hash(line)) if snapshot else ('event', uid or line[:200])
                if key in seen:
                    continue
                seen.add(key)
                event['_session'] = session
                events.append(event)
    return events, ids


def lineages(ids):
    """Sessions that share events are one line of work, a continuation after
    a compaction or a crash: each maps to the first of its line."""
    order = list(ids)
    line = {}
    for i, session in enumerate(order):
        if session in line:
            continue
        line[session] = session
        for other in order[i + 1:]:
            if other not in line and ids[session] & ids[other]:
                line[other] = session
    return line


def backup_index(root=BACKUPS):
    """Each backup file by its name, under the session that wrote it."""
    index = {}
    for path in glob.glob(os.path.join(root, '*', '*')):
        session = os.path.basename(os.path.dirname(path))
        index.setdefault(os.path.basename(path), {})[session] = path
    return index


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
    return ''


def timeline(events, target, backups=None):
    """What happened, in time: (moment, session, kind, payload).

    kind is 'request' (text), 'note' (text), 'edit' (the tool's name and
    input), 'bash' (command, cwd, failed, reset, ended: the moment its
    result came back) or 'backup' (path, file)."""
    target = target.rstrip('/')
    backups = backup_index() if backups is None else backups
    failed, resets, ended = set(), {}, {}
    for event in events:
        content = (event.get('message') or {}).get('content')
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get('type') == 'tool_result':
                    ended[c.get('tool_use_id')] = event.get('timestamp')
                    if c.get('is_error'):
                        failed.add(c.get('tool_use_id'))
                    m = RESET.search(text_of(c.get('content')))
                    if m:
                        resets[c.get('tool_use_id')] = m.group(1)
    items = []
    for event in events:
        session, moment = event['_session'], event.get('timestamp', '')
        content = (event.get('message') or {}).get('content')
        kind = event.get('type')
        if kind == 'file-history-snapshot':
            for path, info in ((event.get('snapshot') or {}).get('trackedFileBackups') or {}).items():
                name = info.get('backupFileName')
                if not os.path.isabs(path) and info.get('realParentDir'):
                    path = os.path.join(info['realParentDir'], os.path.basename(path))
                if not name or not (path + '/').startswith(target + '/'):
                    continue
                kept = backups.get(name, {})
                file = kept.get(session) or next(iter(kept.values()), None)
                if file:
                    items.append((info.get('backupTime', moment), session, 'backup', (path, file)))
        elif kind == 'user' and not event.get('isMeta') and not event.get('isCompactSummary'):
            if isinstance(content, list) and any(isinstance(c, dict) and c.get('type') == 'tool_result' for c in content):
                continue
            text = text_of(content).strip()
            if text and not text.startswith(NOISE):
                items.append((moment, session, 'request', text))
        elif kind == 'assistant' and isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get('type') == 'text' and c['text'].strip():
                    items.append((moment, session, 'note', c['text']))
                elif c.get('type') == 'tool_use' and c.get('name') in ('Edit', 'Write', 'MultiEdit'):
                    if c['id'] not in failed:
                        items.append((moment, session, 'edit', (c['name'], c.get('input') or {})))
                elif c.get('type') == 'tool_use' and c.get('name') == 'Bash':
                    command = (c.get('input') or {}).get('command', '')
                    items.append((moment, session, 'bash', (command, event.get('cwd') or '', c['id'] in failed,
                                                            resets.get(c['id']), ended.get(c['id']))))
    # a backup kept under several snapshots counts once
    unique, kept = [], set()
    for item in sorted(items, key=lambda i: i[0]):
        if item[2] == 'backup':
            if item[3] in kept:
                continue
            kept.add(item[3])
        unique.append(item)
    return unique
