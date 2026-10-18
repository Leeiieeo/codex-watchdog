"""A foreground application's owned worker; no terminal/session management."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

from .events import EventLog
from .storage import atomic_json, load_state, rotate_file


class Service:
    def __init__(self, config_path, config):
        self.config_path = str(config_path)
        self.state_dir = Path(config['state_dir'])
        self.process = None
        self.lock = None
        self.output = None
        self.retry_at = None
        self.restarts = 0
        self.consecutive_failures = 0
        self.started_at = None
        self.blocked = False
        self.events = None

    def _status(self, phase, **data):
        atomic_json(self.state_dir/'service-status.json', {
            'pid': os.getpid(), 'host': os.uname().nodename, 'phase': phase,
            'worker_pid': self.process.pid if self.process else None,
            'restarts': self.restarts, 'consecutive_failures': self.consecutive_failures,
            'updated_at': time.time(), **data})

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
            load_state(self.state_dir/'state.json')
            (self.state_dir/'STOP').unlink(missing_ok=True)
            self.events = EventLog(self.state_dir)
            self.lock = lock
            self._launch()
        except BaseException:
            if self.lock is not None:
                self.stop()
            else:
                lock.close()
            raise
        self.lock = lock

    def _launch(self):
        source = str(Path(__file__).resolve().parents[1])
        env = {**os.environ, 'PYTHONPATH': source}
        rotate_file(self.state_dir/'service.log')
        self.output = (self.state_dir/'service.log').open('a')
        self.output.write(f'\n[{time.strftime("%Y-%m-%d %H:%M:%S")}] worker start\n')
        self.output.flush()
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-m', 'codex_watchdog.watchdog',
                 '--config', self.config_path, '--parent-pid', str(os.getpid())], env=env, stdin=subprocess.DEVNULL,
                stdout=self.output, stderr=self.output, start_new_session=True)
        except BaseException:
            self.output.close()
            self.output = None
            raise
        self.retry_at = None
        self.started_at = time.monotonic()
        self._status('starting')
        if self.events:
            self.events.record('service_started', restarts=self.restarts)

    def poll(self):
        if self.stop_requested or self.process.poll() is None:
            return None
        if self.blocked:
            return None
        if self.retry_at is None:
            stable = self.started_at is not None and time.monotonic()-self.started_at >= 60
            self.consecutive_failures = 1 if stable else self.consecutive_failures+1
            if self.events:
                self.events.record('service_exited', exit_code=self.process.returncode)
            if self.consecutive_failures >= 3:
                self.blocked = True
                self._status('blocked', reason='守护服务连续启动失败；查看服务日志或运行 doctor')
                if self.events:
                    self.events.record('service_blocked', reason='连续三次快速退出')
                return '守护服务连续启动失败，已停止自动重启；按 D 诊断或查看服务日志'
            delay = min(10*2**(self.consecutive_failures-1), 60)
            self.retry_at = time.monotonic()+delay
            self._status('restarting', retry_at=time.time()+delay)
            return f'守护服务退出（{self.process.returncode}），{delay} 秒后重启；详情见服务日志'
        if time.monotonic() >= self.retry_at:
            self.output.close()
            self.restarts += 1
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
            try:
                if self.started_at is not None:
                    self._status('stopped')
            finally:
                self.lock.close()
                self.lock = None
