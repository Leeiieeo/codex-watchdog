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
from .events import read_metrics


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


def wrap_text(value, width):
    text = ''.join(c if c.isprintable() else ' ' for c in str(value))
    lines, line, used = [], '', 0
    width = max(1, width)
    for char in text:
        size = cells(char)
        if used+size > width and line:
            lines.append(line)
            line, used = '', 0
        line += char
        used += size
    return lines+[line]


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
        if (not isinstance(status, dict) or not isinstance(status.get('targets'), dict)
                or not all(isinstance(row, dict) for row in status['targets'].values())):
            raise ValueError('invalid status file')
    except (OSError, ValueError) as exc:
        error = str(exc)
        status = {}
    checked = status.get('checked_at')
    age = max(0, now-checked) if isinstance(checked, (int,float)) else None
    interval = config['check_interval_seconds']
    next_check = (status.get('next_check_at') if status.get('check_interval_seconds') == interval else None)
    if not isinstance(next_check, (int, float)):
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
    metrics = read_metrics(state_dir)
    for target in config['targets']:
        observed = status.get('targets', {}).get(target['thread_id'], {})
        if observed.get('registration_id', target.get('registration_id')) != target.get('registration_id'):
            observed = {}
        if observed.get('guard_revision') != target.get('guard_revision'):
            observed = {}
        row = {**observed, **target}
        row.setdefault('runtime', 'unknown')
        row.setdefault('goal_status', '-')
        row.setdefault('phase', 'pending_observation')
        row.setdefault('attempts', 0)
        row['lifetime_stats'] = metrics.get(target['thread_id'], {})
        rows.append(row)
    success = sum(not row.get('error') and row.get('phase') != 'pending_observation' for row in rows)
    monitor_health = ('empty' if not rows else 'error' if success == 0 and any(row.get('error') for row in rows)
                      else 'degraded' if any(row.get('error') for row in rows)
                      else 'warming' if success < len(rows) else 'healthy')
    if health in ('stopped', 'stale'):
        monitor_health = health
    elif monitor_health in ('error', 'degraded'):
        health = monitor_health
    try:
        service = json.loads((state_dir/'service-status.json').read_text())
    except (OSError, ValueError):
        service = {}
    if not isinstance(service, dict):
        service = {}
    service_alive = False
    try:
        if service.get('host') == os.uname().nodename and int(service.get('pid', 0)) > 0:
            os.kill(int(service['pid']), 0)
            service_alive = True
    except (OSError, ValueError, TypeError):
        pass
    service_health = ('running' if alive else service.get('phase', 'starting')) if service_alive else ('running' if alive else 'stopped')
    if service_alive and service.get('phase') in ('blocked', 'restarting'):
        service_health = service['phase']
    if (state_dir/'STOP').exists():
        service_health = 'stopped'
    succeeded = [r.get('last_success_at') for r in rows if isinstance(r.get('last_success_at'), (int, float))]
    last_success = max(succeeded) if succeeded else None
    return {'config': config, 'rows': rows, 'health': health, 'age': age,
            'pid': status.get('pid'), 'error': error or status.get('error'),
            'check_interval_seconds':interval,'next_check_at':next_check,
            'service_health': service_health, 'service': service,
            'monitor_health': monitor_health, 'successful_targets': success,
            'last_success_at': last_success,
            'last_success_age': max(0, now-last_success) if isinstance(last_success,(int,float)) else None}


def filter_rows(rows, query):
    query = query.casefold().strip()
    return [r for r in rows if query in ' '.join(str(r.get(k, '')) for k in
            ('label','title','cwd','runtime','goal_status','phase')).casefold()]


def log_lines(config, thread_id=None, service=False):
    try:
        with (Path(config['state_dir'])/('service.log' if service else 'watchdog.log')).open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size-65536)
            stream.seek(start)
            if start: stream.readline()
            lines = stream.read().decode('utf-8', errors='replace').splitlines()
        return list(deque((line for line in lines if thread_id is None or thread_id in line), maxlen=100))
    except OSError as exc:
        return [str(exc)]
