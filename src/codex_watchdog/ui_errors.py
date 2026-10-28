"""Bounded, redacted diagnostics for recoverable UI failures."""
from collections import OrderedDict
from pathlib import Path
import time
import traceback

from .patterns import safe_text
from .storage import rotate_file


class UiErrors:
    def __init__(self):
        self.state_dir = None
        self.recent = OrderedDict()

    def record(self, context, exc):
        message = safe_text(f'{type(exc).__name__}: {exc}')
        key = (context, message)
        now = time.monotonic()
        if now-self.recent.get(key, -float('inf')) < 60:
            return message
        if self.state_dir is not None:
            try:
                path = Path(self.state_dir)/'ui.log'
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                rotate_file(path)
                # Never capture locals; redact exception text and authenticated URLs.
                detail = safe_text(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)), 12000)
                with path.open('a') as stream:
                    stream.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {context}\n{detail}\n')
            except OSError:
                return message+'（界面日志写入失败）'
        self.recent[key] = now
        self.recent.move_to_end(key)
        while len(self.recent) > 64:
            self.recent.popitem(last=False)
        return message
