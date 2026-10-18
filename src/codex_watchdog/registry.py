"""Read-only Codex discovery and serialized edits to the watchdog allowlist."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import uuid

from .rpc import Client
from .storage import atomic_json


def load_config(path):
    path = Path(path).expanduser().resolve()
    config = json.loads(path.read_text())
    if config.get('user_authorized_capacity_resume') is not True:
        raise ValueError('explicit capacity-resume authorization missing in config')
    targets = config.get('targets', [])
    if not isinstance(targets, list) or len({x['thread_id'] for x in targets}) != len(targets):
        raise ValueError('targets must be explicit and unique')
    for target in targets:
        if not all(isinstance(target.get(k), str) and target[k]
                   for k in ('thread_id', 'cwd', 'label')):
            raise ValueError('each target needs thread_id, cwd and label')
    config['targets'] = targets
    for key in ('codex_home', 'socket_path', 'state_dir'):
        value = Path(config[key]).expanduser()
        config[key] = str((path.parent/value).resolve())
    return config


def is_main_thread(thread):
    source = thread.get('source')
    return (not thread.get('parentThreadId')
            and thread.get('threadSource') != 'subAgent'
            and not (isinstance(source, dict) and 'subAgent' in source))


def thread_entry(thread):
    if not is_main_thread(thread):
        raise ValueError('select a main thread, not a child agent')
    if not thread.get('id') or not isinstance(thread.get('cwd'), str) or not thread['cwd']:
        raise ValueError('thread metadata is incomplete')
    return {'thread_id': thread['id'], 'cwd': thread['cwd'],
            'label': Path(thread['cwd']).name or thread['id'],
            'title': thread.get('name') or Path(thread['cwd']).name,
            'runtime': thread.get('status', {}).get('type', 'unknown'),
            'updated_at': thread.get('updatedAt')}


def discover_sessions(config_path):
    config = load_config(config_path)
    managed = {t['thread_id'] for t in config['targets']}
    sessions, warnings = [], []
    client = Client(config['socket_path'])
    try:
        ids = client.request('thread/loaded/list', {}).get('data', [])
        for tid in dict.fromkeys(ids):
            try:
                thread = client.request('thread/read', {'threadId': tid})['thread']
                if thread.get('id') != tid:
                    raise ValueError('thread identity mismatch')
                if not is_main_thread(thread) or thread.get('status', {}).get('type') == 'notLoaded':
                    continue
                row = thread_entry(thread)
                row['managed'] = tid in managed
                sessions.append(row)
            except Exception as exc:
                warnings.append(f'{tid}: {exc}')
    finally:
        client.close()
    return {'sessions': sorted(sessions, key=lambda t: (t['label'].lower(), t['title'], t['thread_id'])),
            'warnings': warnings}


@contextmanager
def config_lock(path):
    path = Path(path).expanduser().resolve()
    with path.with_name(path.name+'.lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield load_config(path)


def add_session(config_path, thread_id, label=None):
    config_path = Path(config_path).expanduser().resolve()
    before = load_config(config_path)
    client = Client(before['socket_path'])
    try:
        thread = client.request('thread/read', {'threadId': thread_id})['thread']
        if thread.get('id') != thread_id:
            raise ValueError('thread identity mismatch')
        entry = thread_entry(thread)
        if entry['runtime'] == 'notLoaded':
            raise ValueError('session is no longer loaded; refresh the list')
    finally:
        client.close()
    with config_lock(config_path) as config:
        if any(config[k] != before[k] for k in ('socket_path', 'codex_home', 'state_dir')):
            raise ValueError('connection settings changed; refresh before adding')
        for target in config['targets']:
            if target['thread_id'] == thread_id:
                if target['cwd'] != entry['cwd']:
                    raise ValueError('managed thread binding changed')
                return target  # Repeated add must never reset a recovery baseline.
        target = {k: entry[k] for k in ('thread_id', 'cwd', 'label', 'title')}
        target['label'] = label or target['label']
        target['registration_id'] = str(uuid.uuid4())
        config['targets'].append(target)
        atomic_json(config_path, config)
    return target


def remove_session(config_path, thread_id, registration_id=None):
    config_path = Path(config_path).expanduser().resolve()
    with config_lock(config_path) as config:
        target = next((t for t in config['targets'] if t['thread_id'] == thread_id), None)
        if target is None:
            return False
        if registration_id is not None and target.get('registration_id') != registration_id:
            raise ValueError('session registration changed; refresh before removing')
        config['targets'] = [t for t in config['targets'] if t['thread_id'] != thread_id]
        atomic_json(config_path, config)
    return True
