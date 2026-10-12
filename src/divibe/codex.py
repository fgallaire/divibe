"""Codex CLI's records of a project.

Codex writes one JSONL file per session under ~/.codex/sessions (and
~/.codex/archived_sessions), one event per line. The events this reads are
the items a turn completed:

- a UserMessage: a request;
- an AgentMessage: what the agent said;
- a CommandExecution: a command, the directory it ran in, its exit code and
  how long it took;
- a FileChange: each file added with its content, updated with a unified
  diff (and moved, maybe), or deleted.

Codex keeps no copy of a file before it changes it.
"""
import datetime
import glob
import json
import os
import urllib.parse

HOME = os.path.expanduser('~/.codex')


def session_files(target, home=HOME):
    """Every session file that names the target directory."""
    inside, itself = target.rstrip('/') + '/', json.dumps(target.rstrip('/'))
    found = []
    for folder in ('sessions', 'archived_sessions'):
        for path in sorted(glob.glob(os.path.join(home, folder, '**', '*.jsonl'), recursive=True)):
            with open(path, encoding='utf-8', errors='replace') as f:
                if any(inside in line or itself in line for line in f):
                    found.append(path)
    return found


def text_of(content):
    return '\n'.join(c.get('text', '') for c in content or () if isinstance(c, dict)).strip()


def earlier(moment, duration):
    """A log's moment, minus a Codex duration ({secs, nanos})."""
    if not isinstance(duration, dict):
        return moment
    when = datetime.datetime.fromisoformat(moment.replace('Z', '+00:00'))
    when -= datetime.timedelta(seconds=duration.get('secs', 0), microseconds=duration.get('nanos', 0) // 1000)
    return when.strftime('%Y-%m-%dT%H:%M:%S.') + f'{when.microsecond // 1000:03d}Z'


def timeline(files):
    """What happened, in time, as logs.timeline gives it; a file change is
    an 'edit' of the tool 'Patch'. A session forked from another copies its
    items: each one counts once."""
    items, seen = [], set()
    for path in files:
        session = os.path.basename(path)
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get('payload') or {}
                if event.get('type') == 'session_meta':
                    session = 'codex ' + str(payload.get('id') or session)
                    continue
                if payload.get('type') != 'item_completed':
                    continue
                item = payload.get('item') or {}
                if item.get('id'):
                    if item['id'] in seen:
                        continue
                    seen.add(item['id'])
                moment, kind = event.get('timestamp', ''), item.get('type')
                if kind == 'UserMessage' and text_of(item.get('content')):
                    items.append((moment, session, 'request', text_of(item.get('content'))))
                elif kind == 'AgentMessage' and text_of(item.get('content')):
                    items.append((moment, session, 'note', text_of(item.get('content'))))
                elif kind == 'CommandExecution':
                    command = item.get('command')
                    command = command[-1] if isinstance(command, list) and command else str(command or '')
                    cwd = item.get('cwd') or ''
                    if cwd.startswith('file://'):
                        cwd = urllib.parse.unquote(urllib.parse.urlparse(cwd).path)
                    failed = item.get('status') != 'completed' or item.get('exit_code') not in (0, None)
                    items.append((earlier(moment, item.get('duration')), session, 'bash',
                                  (command, cwd, failed, None, moment)))
                elif kind == 'FileChange' and item.get('status') == 'completed':
                    for target, change in (item.get('changes') or {}).items():
                        items.append((moment, session, 'edit', ('Patch', dict(change, file_path=target))))
    return sorted(items, key=lambda i: i[0])
