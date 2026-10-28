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
from .patterns import safe_text
from .view_models import (DashboardView, Observation, ViewDataError, number_value,
                          object_value, session_view, text_value)


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


def read_status(path):
    """An absent snapshot is pending; an unreadable snapshot is an error."""
    try:
        return object_value(json.loads(path.read_text()), path.name), None
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError, UnicodeError) as exc:
        return {}, safe_text(str(exc))


def process_alive(status):
    try:
        pid = number_value(status.get('pid'), 'pid', 0, integer=True)
        if status.get('host') == os.uname().nodename and pid > 0:
            os.kill(pid, 0)
            return True
    except (OSError, ValueError, OverflowError):
        pass
    return False


def dashboard(config_path, now=None):
    now = time.time() if now is None else now
    config = load_config(config_path)
    state_dir = Path(config['state_dir'])
    status, error = read_status(state_dir/'status.json')
    interval = config['check_interval_seconds']
    checked, next_check, pid, targets = None, None, None, {}
    try:
        targets = object_value(status.get('targets', {}), 'status.targets')
        checked = number_value(status.get('checked_at'), 'checked_at')
        pid = number_value(status.get('pid'), 'pid', integer=True)
        if status.get('check_interval_seconds') == interval:
            next_check = number_value(status.get('next_check_at'), 'next_check_at')
        error = error or text_value(status.get('error'), 'status.error')
    except ViewDataError as exc:
        error = str(exc)
    if next_check is None and checked is not None:
        next_check = checked+interval
    age = max(0, now-checked) if checked is not None else None
    alive = process_alive(status)
    stopped = (state_dir/'STOP').exists()
    health = ('stopped' if stopped or not alive else
              'stale' if next_check is None or now > next_check+20 else
              'error' if error else 'running')
    rows = []
    metrics = read_metrics(state_dir)
    for target in config['targets']:
        observed = targets.get(target['thread_id'], {})
        rows.append(session_view(target, observed, metrics.get(target['thread_id'], {}),
                                 stale=health in ('stopped', 'stale'), error=error))
    success = sum(row.observation == Observation.CURRENT for row in rows)
    failures = any(row.observation == Observation.FAILED for row in rows)
    monitor_health = ('empty' if not rows else 'error' if success == 0 and failures
                      else 'degraded' if failures
                      else 'warming' if success < len(rows) else 'healthy')
    if health in ('stopped', 'stale'):
        monitor_health = health
    elif monitor_health in ('error', 'degraded'):
        health = monitor_health
    service, service_error = read_status(state_dir/'service-status.json')
    service_alive = process_alive(service)
    try:
        service_phase = text_value(service.get('phase'), 'service.phase') or 'starting'
    except ViewDataError as exc:
        service_phase, service_error = 'unknown', str(exc)
    service_health = ('running' if alive else service_phase) if service_alive else ('running' if alive else 'stopped')
    if service_alive and service_phase in ('blocked', 'restarting'):
        service_health = service_phase
    if stopped:
        service_health = 'stopped'
    succeeded = [row.last_success_at for row in rows if row.last_success_at is not None]
    last_success = max(succeeded) if succeeded else None
    return DashboardView(config=config, rows=tuple(rows), health=health, age=age,
                         pid=pid, error=safe_text(error or service_error) if error or service_error else None,
                         check_interval_seconds=interval, next_check_at=next_check,
                         service_health=service_health, service=service,
                         monitor_health=monitor_health, successful_targets=success,
                         last_success_at=last_success,
                         last_success_age=max(0, now-last_success) if last_success is not None else None)


def filter_rows(rows, query):
    query = query.casefold().strip()
    return [row for row in rows if query in ' '.join(
        (row.label, row.title, row.cwd, row.runtime or '', row.goal.status or '', row.phase)).casefold()]


def log_lines(config, thread_id=None, service=False, source=None):
    source = source or ('service' if service else 'watchdog')
    if source not in ('watchdog', 'service', 'ui'):
        raise ValueError('unknown log source')
    try:
        with (Path(config['state_dir'])/(source+'.log')).open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size-65536)
            stream.seek(start)
            if start: stream.readline()
            lines = stream.read().decode('utf-8', errors='replace').splitlines()
        return list(deque((line for line in lines if thread_id is None or thread_id in line), maxlen=100))
    except OSError as exc:
        return [str(exc)]
