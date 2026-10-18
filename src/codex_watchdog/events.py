"""Bounded, durable event history and lifetime recovery metrics."""
from collections import deque
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from .patterns import safe_text
from .storage import atomic_json, rotate_file

EVENT_NAMES = {
    'failure_detected': '检测到失败', 'recovery_scheduled': '安排恢复',
    'recovery_cancelled': '取消恢复', 'resume_attempted': '提交恢复',
    'resume_accepted': '恢复已接受', 'resume_unknown': '恢复结果不明确',
    'recovery_running': '观察到新轮次运行', 'recovery_completed': '恢复后的轮次完成',
    'goal_completed': 'Goal 完成', 'guard_paused': '暂停守护',
    'guard_resumed': '重新启用守护', 'observation_failed': '读取失败',
    'observation_restored': '读取恢复', 'attention': '需要关注',
    'service_started': '服务启动', 'service_exited': '服务退出',
    'service_blocked': '服务停止自动重启', 'notification': '通知',
    'notification_failed': '通知发送失败',
}
COUNTERS = {'failure_detected': 'failures', 'resume_attempted': 'attempts',
            'resume_accepted': 'accepted', 'recovery_completed': 'recoveries',
            'goal_completed': 'completed_goals'}


class EventLog:
    def __init__(self, state_dir, observe=False):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prefix = 'observe-' if observe else ''
        self.path = self.root/(self.prefix+'events.jsonl')

    @contextmanager
    def locked(self):
        with (self.root/(self.prefix+'events.lock')).open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield

    def record(self, kind, thread_id=None, now=None, **data):
        def scrub(value):
            if isinstance(value, str):
                return safe_text(value)
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [scrub(v) for v in value]
            return value
        event = {'id': str(uuid.uuid4()), 'at': time.time() if now is None else now,
                 'kind': kind, 'thread_id': thread_id,
                 **scrub(data)}
        with self.locked():
            rotate_file(self.path)
            with self.path.open('a') as stream:
                stream.write(json.dumps(event, ensure_ascii=False)+'\n')
                stream.flush()
                os.fsync(stream.fileno())
            self._metrics(event)
        return event

    def _metrics(self, event):
        path = self.root/(self.prefix+'metrics.json')
        try:
            metrics = json.loads(path.read_text()) if path.exists() else {'targets': {}}
            if (not isinstance(metrics, dict) or not isinstance(metrics.get('targets'), dict)
                    or not all(isinstance(row, dict) and all(isinstance(v,(int,float)) for v in row.values())
                               for row in metrics['targets'].values())):
                raise ValueError('invalid metrics')
        except (ValueError, TypeError):
            # Preserve damaged derived data; recovery state is never reset.
            os.replace(path, path.with_name(path.name+'.corrupt-'+str(time.time_ns())))
            metrics = {'targets': {}}
            for old in read_events(self.root, limit=100000, observe=bool(self.prefix)):
                self._apply_metrics(metrics, old)
            atomic_json(path, metrics)
            return
        self._apply_metrics(metrics, event)
        atomic_json(path, metrics)

    @staticmethod
    def _apply_metrics(metrics, event):
        tid = event.get('thread_id')
        if tid:
            row = metrics['targets'].setdefault(tid, {})
            counter = COUNTERS.get(event['kind'])
            if counter:
                row[counter] = row.get(counter, 0)+1
            if event['kind'] == 'recovery_running':
                duration = event.get('downtime_seconds', 0)
                row['downtime_seconds'] = row.get('downtime_seconds', 0)+duration
            row['last_event_at'] = event['at']


def read_events(state_dir, thread_id=None, limit=200, observe=False):
    root = Path(state_dir)
    name = ('observe-' if observe else '')+'events.jsonl'
    result = deque(maxlen=limit)
    for suffix in ('.3', '.2', '.1', ''):
        try:
            with (root/(name+suffix)).open('rb') as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size-1024*1024))
                if size > 1024*1024:
                    stream.readline()
                for line in stream:
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and (thread_id is None or event.get('thread_id') == thread_id):
                            result.append(event)
                    except (ValueError, UnicodeError):
                        continue
        except OSError:
            pass
    return list(result)


def read_metrics(state_dir):
    try:
        data = json.loads((Path(state_dir)/'metrics.json').read_text())
        return data['targets'] if isinstance(data, dict) and isinstance(data.get('targets'), dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
