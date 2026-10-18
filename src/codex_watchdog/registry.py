"""Read-only Codex discovery and serialized edits to the watchdog allowlist."""
from contextlib import contextmanager
from decimal import Decimal, DecimalException, ROUND_HALF_UP
import fcntl
import json
from pathlib import Path
import uuid

from .rpc import Client, RpcError
from .storage import atomic_json
from .patterns import PATTERNS, DEFAULT_MODES

DEFAULT_CHECK_INTERVAL = 300


def validate_check_interval(seconds):
    if isinstance(seconds,bool) or not isinstance(seconds,int) or not 1 <= seconds <= 86400:
        raise ValueError('check interval must be an integer between 1 and 86400 seconds')
    return seconds


def minutes_to_seconds(text):
    try:
        minutes = Decimal(text)
        if not minutes.is_finite() or not Decimal(1)/60 <= minutes <= 1440:
            raise ValueError('请输入 1 秒至 1440 分钟之间的时间')
        return validate_check_interval(int((minutes*60).to_integral_value(rounding=ROUND_HALF_UP)))
    except (DecimalException,TypeError) as exc:
        raise ValueError('请输入分钟数，例如 5 或 0.5') from exc


def load_config(path):
    path = Path(path).expanduser().resolve()
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or config.get('schema_version', 1) != 1:
        raise ValueError('unsupported configuration schema; run doctor')
    if not isinstance(config.get('observe_only', False), bool):
        raise ValueError('observe_only must be true or false')
    if not config.get('observe_only') and config.get('user_authorized_capacity_resume') is not True:
        raise ValueError('explicit capacity-resume authorization missing in config')
    targets = config.get('targets', [])
    if (not isinstance(targets, list) or not all(isinstance(x, dict) for x in targets)
            or not all(isinstance(x.get('thread_id'), str) for x in targets)
            or len({x['thread_id'] for x in targets}) != len(targets)):
        raise ValueError('targets must be explicit and unique')
    for target in targets:
        if not all(isinstance(target.get(k), str) and target[k]
                   for k in ('thread_id', 'cwd', 'label')):
            raise ValueError('each target needs thread_id, cwd and label')
        if not isinstance(target.get('enabled', True), bool):
            raise ValueError('target enabled must be true or false')
    modes = config.get('pattern_modes', {})
    if not isinstance(modes, dict) or set(modes)-set(DEFAULT_MODES):
        raise ValueError('unknown failure pattern in pattern_modes')
    for pattern in PATTERNS:
        mode = modes.get(pattern.id, DEFAULT_MODES[pattern.id])
        if mode not in ('recover', 'observe', 'off') or (mode == 'recover' and not pattern.recoverable):
            raise ValueError('only capacity failures support recover mode')
    for key, default, minimum in (('stall_after_seconds', 0, 0),
                                  ('recovery_confirmation_seconds', 300, 1),
                                  ('attention_after_seconds', 1800, 1)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 604800:
            raise ValueError(key+' must be an integer within the supported range')
    notifications = config.get('notifications', {})
    if not isinstance(notifications, dict):
        raise ValueError('notifications must be an object')
    for key in ('enabled', 'terminal_bell'):
        if not isinstance(notifications.get(key, False), bool):
            raise ValueError('notification '+key+' must be true or false')
    env_name = notifications.get('webhook_url_env', 'CODEX_WATCHDOG_WEBHOOK_URL')
    if not isinstance(env_name, str) or not env_name.isidentifier():
        raise ValueError('webhook_url_env must name an environment variable')
    cooldown = notifications.get('cooldown_seconds', 300)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or not 1 <= cooldown <= 86400:
        raise ValueError('notification cooldown must be 1 to 86400 seconds')
    config['targets'] = targets
    config['check_interval_seconds'] = validate_check_interval(
        config.get('check_interval_seconds',DEFAULT_CHECK_INTERVAL))
    for key in ('codex_home', 'socket_path', 'state_dir'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(key+' must be a nonempty path')
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
                if not thread.get('name'):
                    # The daemon also lists empty draft threads belonging to
                    # open clients. They cannot be inspected or guarded yet.
                    try:
                        turns = client.request('thread/turns/list', {
                            'threadId': tid, 'limit': 1, 'sortDirection': 'desc',
                            'itemsView': 'notLoaded'})['data']
                    except RpcError as exc:
                        if 'not materialized yet' in str(exc):
                            continue
                        raise
                    if not turns:
                        continue
                row = thread_entry(thread)
                row['managed'] = tid in managed
                sessions.append(row)
            except Exception as exc:
                warnings.append(f'{tid}: {exc}')
    finally:
        client.close()
    return {'sessions': sorted(sessions, key=lambda t: (
                t['runtime'] != 'active', t['label'].lower(), -(t.get('updated_at') or 0),
                t['title'], t['thread_id'])),
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


def set_check_interval(config_path, seconds):
    seconds = validate_check_interval(seconds)
    config_path = Path(config_path).expanduser().resolve()
    with config_lock(config_path) as config:
        config['check_interval_seconds'] = seconds
        atomic_json(config_path, config)
    return seconds


def set_session_enabled(config_path, thread_id, enabled, registration_id=None):
    if not isinstance(enabled, bool):
        raise ValueError('enabled must be true or false')
    config_path = Path(config_path).expanduser().resolve()
    with config_lock(config_path) as config:
        target = next((t for t in config['targets'] if t['thread_id'] == thread_id), None)
        if target is None:
            raise ValueError('session is no longer managed')
        if registration_id is not None and target.get('registration_id') != registration_id:
            raise ValueError('session registration changed; refresh before toggling')
        if target.get('enabled', True) != enabled:
            target.update(enabled=enabled, guard_revision=str(uuid.uuid4()))
            atomic_json(config_path, config)
    return target


def set_pattern_mode(config_path, pattern_id, mode):
    config_path = Path(config_path).expanduser().resolve()
    with config_lock(config_path) as config:
        candidate = next((p for p in PATTERNS if p.id == pattern_id), None)
        if candidate is None or mode not in ('recover', 'observe', 'off'):
            raise ValueError('invalid failure pattern or mode')
        if mode == 'recover' and not candidate.recoverable:
            raise ValueError('this failure pattern only supports observation')
        config.setdefault('pattern_modes', {})[pattern_id] = mode
        config['policy_revision'] = str(uuid.uuid4())
        atomic_json(config_path, config)
    return mode


def set_options(config_path, *, notifications=None, terminal_bell=None, stall_after_seconds=None):
    config_path = Path(config_path).expanduser().resolve()
    with config_lock(config_path) as config:
        for key, value in (('enabled', notifications), ('terminal_bell', terminal_bell)):
            if value is not None:
                if not isinstance(value, bool):
                    raise ValueError(key+' must be true or false')
                config.setdefault('notifications', {})[key] = value
        if stall_after_seconds is not None:
            if isinstance(stall_after_seconds, bool) or not isinstance(stall_after_seconds, int) or not 0 <= stall_after_seconds <= 604800:
                raise ValueError('stall threshold must be 0 to 604800 seconds')
            config['stall_after_seconds'] = stall_after_seconds
        atomic_json(config_path, config)
    return config
