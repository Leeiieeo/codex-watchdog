"""Opt-in, rate-limited notifications, dispatched away from the observer."""
import json
import os
from pathlib import Path
import queue
import threading
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .events import EventLog
from .storage import atomic_json


class Notifier:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.events = EventLog(state_dir)
        self.path = self.root/'notifications.json'
        try:
            self.sent = json.loads(self.path.read_text())
            if not isinstance(self.sent, dict):
                self.sent = {}
        except (OSError, ValueError):
            self.sent = {}
        self.sent = {k:v for k,v in self.sent.items() if isinstance(v,(int,float))}
        self.queue = queue.Queue(maxsize=32)
        self.thread = None

    def notify(self, config, thread_id, topic, message, now=None):
        settings = config.get('notifications', {})
        if not settings.get('enabled'):
            return
        now = time.time() if now is None else now
        key = thread_id+':'+topic
        if now-self.sent.get(key, -float('inf')) < settings.get('cooldown_seconds', 300):
            return
        self.sent[key] = now
        atomic_json(self.path, self.sent)
        event = self.events.record('notification', thread_id, now=now,
                                   topic=topic, message=message,
                                   terminal_bell=settings.get('terminal_bell', False))
        url = os.environ.get(settings.get('webhook_url_env', 'CODEX_WATCHDOG_WEBHOOK_URL'))
        if not url:
            return
        try:
            self.queue.put_nowait((url, {'event': topic, 'thread_id': thread_id,
                                        'at': now, 'message': event['message']}))
        except queue.Full:
            self.events.record('notification_failed', thread_id, reason='notification queue full')
            return
        if self.thread is None:
            self.thread = threading.Thread(target=self._dispatch, daemon=True)
            self.thread.start()

    def _dispatch(self):
        while True:
            url, payload = self.queue.get()
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                    raise ValueError('invalid webhook endpoint')
                request = Request(url, data=json.dumps(payload).encode(),
                                  headers={'Content-Type': 'application/json'}, method='POST')
                with urlopen(request, timeout=5) as response:
                    response.read(1024)
            except Exception as exc:
                # Do not log the URL, which may contain a webhook credential.
                try:
                    self.events.record('notification_failed', payload['thread_id'],
                                       reason=type(exc).__name__)
                except OSError:
                    pass
            finally:
                self.queue.task_done()
