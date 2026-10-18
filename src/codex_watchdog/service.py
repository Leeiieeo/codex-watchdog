"""A foreground application's owned worker; no terminal/session management."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time


class Service:
    def __init__(self, config_path, config):
        self.config_path = str(config_path)
        self.state_dir = Path(config['state_dir'])
        self.process = None
        self.lock = None
        self.output = None
        self.retry_at = None

    @property
    def stop_requested(self):
        return (self.state_dir/'STOP').exists()

    def start(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (self.state_dir/'service.lock').open('a+')
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError('Watchdog is already running for this state directory') from None
        try:
            # Also refuse an older or manually launched worker, without clearing
            # its stop sentinel or changing its recovery records.
            with (self.state_dir/'watchdog.lock').open('a+') as worker_lock:
                try:
                    fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError('A watchdog worker is already running; stop it first') from None
            (self.state_dir/'STOP').unlink(missing_ok=True)
            self._launch()
        except BaseException:
            lock.close()
            raise
        self.lock = lock

    def _launch(self):
        source = str(Path(__file__).resolve().parents[1])
        env = {**os.environ, 'PYTHONPATH': source}
        self.output = (self.state_dir/'service.log').open('w')
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-m', 'codex_watchdog.watchdog',
                 '--config', self.config_path], env=env, stdin=subprocess.DEVNULL,
                stdout=self.output, stderr=self.output, start_new_session=True)
        except BaseException:
            self.output.close()
            self.output = None
            raise
        self.retry_at = None

    def poll(self):
        if self.stop_requested or self.process.poll() is None:
            return None
        if self.retry_at is None:
            self.retry_at = time.monotonic()+10
            return f'守护服务退出（{self.process.returncode}），10 秒后重启；详情见 service.log'
        if time.monotonic() >= self.retry_at:
            self.output.close()
            self._launch()
            return '守护服务已重新启动'
        return None

    def stop(self):
        if self.lock is None:
            return
        try:
            try:
                (self.state_dir/'STOP').touch()
            finally:
                if self.process is not None:
                    if self.process.poll() is None:
                        self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
        finally:
            if self.output is not None:
                self.output.close()
            self.lock.close()
            self.lock = None
