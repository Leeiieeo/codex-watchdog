"""Read-only dashboard data and terminal-safe formatting."""
from collections import deque
from datetime import datetime
import json
import math
import os
from pathlib import Path
import time
import unicodedata

from .registry import load_config


def cells(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1
               for c in text)


def fit(value, width):
    text = ''.join(c if c.isprintable() else ' ' for c in str(value))
    result, used = [], 0
    for char in text:
        size = cells(char)
        if used + size > max(0, width): break
        result.append(char)
        used += size
    return ''.join(result) + ' ' * max(0, width-used)


def retry_text(due, now):
    if due is None: return '-'
    seconds = max(0, math.ceil(due-now))
    return f'{seconds//60:02d}:{seconds%60:02d}'


def activity_text(timestamp):
    try:
        return datetime.fromtimestamp(timestamp).strftime('%m-%d %H:%M') if timestamp else '-'
    except (ValueError,TypeError,OSError,OverflowError):
        return '-'


def interval_text(seconds):
    if seconds % 60 == 0: return f'{seconds//60} 分钟'
    if seconds < 60: return f'{seconds} 秒'
    return f'{seconds//60} 分 {seconds%60} 秒'


def dashboard(config_path, now=None):
    now = time.time() if now is None else now
    config = load_config(config_path)
    state_dir = Path(config['state_dir'])
    error, status = None, {}
    try:
        status = json.loads((state_dir/'status.json').read_text())
        if not isinstance(status.get('targets'), dict):
            raise ValueError('invalid status file')
    except (OSError, ValueError) as exc:
        error = str(exc)
        status = {}
    checked = status.get('checked_at')
    age = max(0, now-checked) if isinstance(checked, (int,float)) else None
    interval = config['check_interval_seconds']
    next_check = checked+interval if isinstance(checked,(int,float)) else None
    alive = False
    try:
        if status.get('host') == os.uname().nodename and int(status.get('pid', 0)) > 0:
            os.kill(int(status['pid']), 0)
            alive = True
    except (OSError, ValueError, TypeError):
        pass
    health = ('stopped' if (state_dir/'STOP').exists() or not alive else
              'stale' if next_check is None or now > next_check+20 else
              'error' if error or status.get('error') else 'running')
    rows = []
    for target in config['targets']:
        observed = status.get('targets', {}).get(target['thread_id'], {})
        if observed.get('registration_id', target.get('registration_id')) != target.get('registration_id'):
            observed = {}
        row = {**observed, **target}
        row.setdefault('runtime', 'unknown')
        row.setdefault('goal_status', '-')
        row.setdefault('phase', 'pending_observation')
        row.setdefault('attempts', 0)
        rows.append(row)
    return {'config': config, 'rows': rows, 'health': health, 'age': age,
            'pid': status.get('pid'), 'error': error or status.get('error'),
            'check_interval_seconds':interval,'next_check_at':next_check}


def filter_rows(rows, query):
    query = query.casefold().strip()
    return [r for r in rows if query in ' '.join(str(r.get(k, '')) for k in
            ('label','title','cwd','runtime','goal_status','phase')).casefold()]


def log_lines(config, thread_id=None):
    try:
        with (Path(config['state_dir'])/'watchdog.log').open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size-65536)
            stream.seek(start)
            if start: stream.readline()
            lines = stream.read().decode('utf-8', errors='replace').splitlines()
        return list(deque((line for line in lines if thread_id is None or thread_id in line), maxlen=100))
    except OSError as exc:
        return [str(exc)]
