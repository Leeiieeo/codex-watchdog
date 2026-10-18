"""Durable JSON writes shared by the daemon and registry."""
import json
import math
import os
from pathlib import Path
import tempfile


class StateError(ValueError):
    pass


def load_state(path):
    """Read legacy state without discarding deduplication records on upgrade."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError('state must be an object')
        if 'schema_version' in raw:
            if raw['schema_version'] != 1 or not isinstance(raw.get('targets'), dict):
                raise ValueError('unsupported state version')
            raw = raw['targets']
        for item in raw.values():
            if not isinstance(item, dict):
                raise ValueError('invalid target state')
            if not item:
                continue
            if (not isinstance(item.get('attempted'), list)
                    or not all(isinstance(x, str) for x in item['attempted'])
                    or not isinstance(item.get('attempts'), int)
                    or isinstance(item['attempts'], bool) or item['attempts'] < 0
                    or not isinstance(item.get('last_turn'), list)
                    or len(item['last_turn']) != 3
                    or not isinstance(item.get('first_seen'), (int, float))
                    or not math.isfinite(item['first_seen'])):
                raise ValueError('invalid recovery or deduplication record')
            pending = item.get('pending')
            if pending is not None and (not isinstance(pending, dict)
                    or not isinstance(pending.get('due'), (int, float))
                    or not math.isfinite(pending['due'])
                    or not isinstance(pending.get('fingerprint'), list)
                    or len(pending['fingerprint']) != 6):
                raise ValueError('invalid pending recovery')
            if pending and not isinstance(pending['fingerprint'][-1], str):
                from .patterns import error_fingerprint
                pending['fingerprint'][-1] = error_fingerprint(pending['fingerprint'][-1])
            recovery = item.get('recovery')
            if recovery is not None and (not isinstance(recovery, dict)
                    or not isinstance(recovery.get('failed_turn'), str)
                    or recovery.get('stage') not in ('awaiting','running','completed')
                    or not isinstance(recovery.get('attempted_at'), (int,float))
                    or not isinstance(recovery.get('failure_at'), (int,float))):
                raise ValueError('invalid recovery confirmation record')
            if not isinstance(item.get('stats', {}), dict):
                raise ValueError('invalid target metrics')
        return raw
    except (ValueError, TypeError, KeyError) as exc:
        raise StateError('Recovery state cannot be loaded; original file preserved. '
                         'Run doctor and restore a verified backup: '+str(exc)) from exc


def save_state(path, targets):
    atomic_json(path, {'schema_version': 1, 'targets': targets})


def rotate_file(path, max_bytes=1024*1024, backups=3):
    path = Path(path)
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    path.with_name(path.name+f'.{backups}').unlink(missing_ok=True)
    for number in range(backups-1, 0, -1):
        source = path.with_name(path.name+f'.{number}')
        if source.exists():
            os.replace(source, path.with_name(path.name+f'.{number+1}'))
    os.replace(path, path.with_name(path.name+'.1'))


def atomic_json(path, value):
    path = Path(path)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent,
                                         prefix=path.name+'.', delete=False) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()
