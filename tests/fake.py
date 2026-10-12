"""Session logs and file backups made up for the tests, in the shape Claude
Code writes them."""
import itertools
import json
import os

_ids = itertools.count(1)


def uid():
    return f'id-{next(_ids):05d}'


class Session:
    """One session log, written event by event, with a clock that advances
    a minute per event unless told the moment."""

    def __init__(self, root, project_dir, cwd, name=None, start='2026-01-01T10:00:00'):
        self.root = root
        self.name = name or uid()
        self.cwd = cwd
        self.events = []
        self.minute = 0
        self.start = start
        folder = os.path.join(root, 'projects', project_dir)
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, self.name + '.jsonl')

    def moment(self):
        self.minute += 1
        hours, minutes = divmod(self.minute, 60)
        day, time = self.start.split('T')
        h, m, s = (int(x) for x in time.split(':'))
        return f'{day}T{h + hours:02d}:{m + minutes:02d}:{s:02d}.000Z'

    def add(self, event):
        event.setdefault('timestamp', self.moment())
        event.setdefault('uuid', uid())
        event.setdefault('sessionId', self.name)
        event.setdefault('cwd', self.cwd)
        self.events.append(event)
        return event

    def ask(self, text):
        return self.add({'type': 'user', 'message': {'role': 'user', 'content': text}})

    def say(self, text):
        return self.add({'type': 'assistant', 'message': {'role': 'assistant',
                                                          'content': [{'type': 'text', 'text': text}]}})

    def call(self, name, tool_input, result='ok', error=False):
        use = uid()
        self.add({'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': use, 'name': name, 'input': tool_input}]}})
        self.add({'type': 'user', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': use, 'content': result, 'is_error': error}]}})
        return use

    def write(self, path, content, **kw):
        return self.call('Write', {'file_path': path, 'content': content}, **kw)

    def edit(self, path, old, new, **kw):
        return self.call('Edit', {'file_path': path, 'old_string': old, 'new_string': new}, **kw)

    def bash(self, command, **kw):
        return self.call('Bash', {'command': command}, **kw)

    def backup(self, path, content, version=1):
        """A snapshot that backs up a file as it is now, under the prompt of
        the last request."""
        name = f'{abs(hash(path)) % 10 ** 16:016x}@v{version}'
        folder = os.path.join(self.root, 'file-history', self.name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
            f.write(content)
        moment = self.moment()
        rel = os.path.basename(path)
        self.events.append({'type': 'file-history-snapshot', 'messageId': uid(), 'snapshot': {
            'trackedFileBackups': {rel: {'backupFileName': name, 'version': version, 'backupTime': moment,
                                         'realParentDir': os.path.dirname(path)}}}})

    def save(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            for event in self.events:
                f.write(json.dumps(event) + '\n')
        return self.path
