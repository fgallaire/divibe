"""OpenCode's records of a project.

OpenCode keeps its sessions in a SQLite database,
~/.local/share/opencode/opencode.db: a session has its directory and, for
a subagent's, its parent; a message has its role; a part is a piece of a
message. The parts this reads:

- the text of the user's message: a request (a text OpenCode adds itself is
  marked synthetic, and a question asked by the way, /btw, is a note);
- the text of the agent's message: what it said;
- the tools that change files: write (the whole content), edit and
  multiedit (old and new text; an empty old text writes the file), and bash
  (a command, its directory, how it ended).

The database is opened read only. OpenCode keeps no copy of a file before
it changes it.
"""
import datetime
import json
import os
import sqlite3
import urllib.parse

DATABASE = os.path.expanduser('~/.local/share/opencode/opencode.db')


def iso(ms):
    """A moment in milliseconds as a log's moment."""
    when = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    return when.strftime('%Y-%m-%dT%H:%M:%S.') + f'{int(ms) % 1000:03d}Z'


def change(tool, args, directory):
    """A file tool's input as logs.timeline gives an edit, None for another
    tool."""
    path = args.get('filePath') or ''
    if path and not os.path.isabs(path):
        path = os.path.join(directory, path)
    if tool == 'write' or tool == 'edit' and args.get('oldString') == '':
        return 'Write', {'file_path': path, 'content': args.get('content', args.get('newString', ''))}
    if tool == 'edit':
        return 'Edit', {'file_path': path, 'old_string': args.get('oldString', ''),
                        'new_string': args.get('newString', ''), 'replace_all': bool(args.get('replaceAll'))}
    if tool == 'multiedit':
        return 'MultiEdit', {'file_path': path, 'edits': [
            {'old_string': e.get('oldString', ''), 'new_string': e.get('newString', ''),
             'replace_all': bool(e.get('replaceAll'))} for e in args.get('edits') or ()]}
    return None


def timeline(target, database=DATABASE):
    """The sessions that name the target directory and what happened in
    them, in time, as logs.timeline gives it: (session count, items,
    lines). A subagent's session is a line of its parent's work, and what it
    was asked is a note, not a request."""
    if not os.path.exists(database):
        return 0, [], {}
    target = target.rstrip('/')
    db = sqlite3.connect(f'file:{urllib.parse.quote(database)}?mode=ro', uri=True)
    try:
        sessions = {sid: (directory or '', parent) for sid, directory, parent
                    in db.execute('select id, directory, parent_id from session')}

        def root(sid):
            seen = set()
            while sessions.get(sid, ('', None))[1] and sid not in seen:
                seen.add(sid)
                sid = sessions[sid][1]
            return sid
        named = {sid for sid, (directory, _) in sessions.items()
                 if directory == target or directory.startswith(target + '/')}
        named |= {sid for (sid,) in db.execute('select distinct session_id from part where instr(data, ?) > 0',
                                               (target + '/',))}
        roots = {root(sid) for sid in named}
        chosen = sorted(sid for sid in sessions if root(sid) in roots)
        if not chosen:
            return 0, [], {}
        lines = {'opencode ' + sid: 'opencode ' + root(sid) for sid in chosen}
        marks = ','.join('?' * len(chosen))
        roles = {mid: json.loads(data).get('role') for mid, data
                 in db.execute(f'select id, data from message where session_id in ({marks})', chosen)}
        rows = list(db.execute(f'select session_id, message_id, time_created, data from part '
                               f'where session_id in ({marks}) order by time_created, id', chosen))
    finally:
        db.close()
    items = []
    for sid, mid, created, data in rows:
        part = json.loads(data)
        session, directory = 'opencode ' + sid, sessions[sid][0]
        if part.get('type') == 'text' and part.get('text', '').strip() and not part.get('synthetic'):
            # a question asked by the way (/btw) is no new piece of work
            asked = roles.get(mid) == 'user' and not sessions[sid][1] and not part['text'].startswith('/btw')
            items.append((iso((part.get('time') or {}).get('start') or created), session,
                          'request' if asked else 'note', part['text']))
        elif part.get('type') == 'tool':
            state = part.get('state') or {}
            args, times = state.get('input') or {}, state.get('time') or {}
            start, end = times.get('start') or created, times.get('end')
            if part.get('tool') == 'bash':
                failed = state.get('status') != 'completed' or (state.get('metadata') or {}).get('exit') not in (None, 0)
                items.append((iso(start), session, 'bash', (args.get('command', ''), args.get('workdir') or directory,
                                                            failed, None, iso(end) if end else None)))
            elif state.get('status') == 'completed':
                edit = change(part.get('tool'), args, directory)
                if edit:
                    items.append((iso(end or start), session, 'edit', edit))
    return len(chosen), sorted(items, key=lambda i: i[0]), lines
