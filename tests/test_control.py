import curses
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import Mock, patch

from codex_watchdog import control
from codex_watchdog.registry import load_config
from codex_watchdog.service import Service

ROOT = Path(__file__).resolve().parents[1]


def controlling_terminal():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'config.json'
        self.path.write_text(json.dumps({
            'user_authorized_capacity_resume': True,
            'codex_home': self.tmp.name, 'socket_path': self.tmp.name+'/socket',
            'state_dir': self.tmp.name+'/state', 'targets': []}))
        self.config = load_config(self.path)


class ControlTests(ConfigTestCase):
    def test_default_start_and_tui_use_the_same_application(self):
        for action in ([], ['start'], ['tui']):
            with self.subTest(action=action), patch.object(control, 'run_application') as run:
                with patch.object(sys, 'argv', ['codex-watchdog', '--config', str(self.path), *action]):
                    control.main()
                run.assert_called_once_with(self.path.resolve(), self.config)

    def test_terminal_failure_does_not_start_a_worker(self):
        service = Mock()
        with patch.object(control, 'Service', return_value=service), \
                patch.object(control.curses, 'wrapper', side_effect=curses.error('no terminal')):
            with self.assertRaises(curses.error):
                control.run_application(self.path, self.config)
        service.start.assert_not_called()
        service.stop.assert_called_once()

    def test_tui_failure_stops_worker_and_restores_signal_handlers(self):
        service = Mock()
        app = Mock()
        app.quit = False
        app.run.side_effect = RuntimeError('draw failed')
        handlers = {sig: signal.getsignal(sig) for sig in
                    (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with patch.object(control, 'Service', return_value=service), \
                patch('codex_watchdog.tui.App', return_value=app), \
                patch.object(control.curses, 'wrapper', side_effect=lambda fn: fn(Mock())):
            with self.assertRaisesRegex(RuntimeError, 'draw failed'):
                control.run_application(self.path, self.config)
        service.start.assert_called_once()
        service.stop.assert_called_once()
        self.assertEqual(handlers, {sig: signal.getsignal(sig) for sig in handlers})

    def test_existing_worker_is_not_modified_by_failed_start(self):
        state_dir = Path(self.config['state_dir'])
        state_dir.mkdir()
        (state_dir/'STOP').touch()
        service = Service(self.path, self.config)
        with (state_dir/'watchdog.lock').open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, 'worker is already running'):
                service.start()
        service.stop()
        self.assertTrue((state_dir/'STOP').exists())
        self.assertIsNone(service.process)

    def test_unresponsive_worker_is_killed_and_reaped_on_shutdown(self):
        state_dir = Path(self.config['state_dir'])
        state_dir.mkdir()
        service = Service(self.path, self.config)
        service.lock = Mock()
        lock = service.lock
        service.output = Mock()
        service.process = Mock()
        service.process.pid = 123
        service.process.poll.return_value = None
        service.process.wait.side_effect = [subprocess.TimeoutExpired('worker', 10), 0]
        service.stop()
        service.process.terminate.assert_called_once()
        service.process.kill.assert_called_once()
        self.assertEqual(service.process.wait.call_count, 2)
        lock.close.assert_called_once()
        self.assertIsNone(service.lock)
        self.assertTrue((state_dir/'STOP').exists())

    def test_worker_is_restarted_only_after_delay_and_not_after_stop(self):
        service = Service(self.path, self.config)
        service.process = Mock(returncode=1)
        service.process.poll.return_value = 1
        service.output = Mock()
        service._status = Mock()
        with patch('codex_watchdog.service.time.monotonic', return_value=100), \
                patch.object(service, '_launch') as launch:
            self.assertIn('10 秒', service.poll())
            launch.assert_not_called()
        with patch('codex_watchdog.service.time.monotonic', return_value=110), \
                patch.object(service, '_launch') as launch:
            service.poll()
            launch.assert_called_once()
        Path(self.config['state_dir']).mkdir()
        (Path(self.config['state_dir'])/'STOP').touch()
        with patch.object(service, '_launch') as launch:
            self.assertIsNone(service.poll())
            launch.assert_not_called()


class TerminalTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.proc = None
        self.master = None
        self.worker_pid = None
        self.output = bytearray()

    def tearDown(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.master is not None:
            os.close(self.master)
        if self.worker_pid is not None:
            try:
                os.kill(self.worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def read_until(self, predicate, timeout=5):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            ready, _, _ = select.select([self.master], [], [], .05)
            if ready:
                try:
                    self.output.extend(os.read(self.master, 65536))
                except OSError:
                    break
        self.assertTrue(predicate(), self.output.decode('utf-8', errors='replace'))

    def launch(self, command, theme=None):
        # A fake tmux executable fails if startup tries to invoke it.
        tools = Path(self.tmp.name)/'bin'
        tools.mkdir()
        trap = tools/'tmux'
        marker = Path(self.tmp.name)/'tmux-called'
        trap.write_text('#!/bin/sh\ntouch "$CODEX_WATCHDOG_TEST_TMUX_MARKER"\nexit 99\n')
        trap.chmod(0o700)
        env = {**os.environ, 'TERM': 'xterm-256color', 'PYTHONPATH': str(ROOT/'src'),
               'CODEX_WATCHDOG_CONFIG': str(self.path), 'CODEX_WATCHDOG_PYTHON': sys.executable,
               'CODEX_WATCHDOG_TEST_TMUX_MARKER': str(marker),
               'PATH': str(tools)+os.pathsep+os.environ.get('PATH', '')}
        if theme:
            env.pop('NO_COLOR',None)
            env['CODEX_WATCHDOG_THEME'] = theme
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 130, 0, 0))
        try:
            self.proc = subprocess.Popen(command, cwd=ROOT, env=env,
                                         stdin=slave, stdout=slave, stderr=slave,
                                         preexec_fn=controlling_terminal)
        finally:
            os.close(slave)
        status_path = Path(self.config['state_dir'])/'status.json'
        self.read_until(lambda: b'CODEX WATCHDOG' in self.output and status_path.exists())
        self.worker_pid = json.loads(status_path.read_text())['pid']
        self.assertNotEqual(self.worker_pid, self.proc.pid)
        os.kill(self.worker_pid, 0)
        self.assertFalse(marker.exists())

    def assert_closed(self):
        self.read_until(lambda: self.proc.poll() is not None, timeout=12)
        self.assertEqual(self.proc.wait(), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(self.worker_pid, 0)
        self.assertTrue((Path(self.config['state_dir'])/'STOP').exists())
        self.worker_pid = None

    def test_current_terminal_start_and_double_ctrl_c_close_both(self):
        self.launch([str(ROOT/'control.sh')])
        for key, title in ((b'g','监控规则与提醒设置'),(b'?','键盘帮助'),(b'e','事件时间线'),(b'l','守护日志')):
            self.output.clear()
            os.write(self.master,key)
            self.read_until(lambda: title.encode() in self.output)
            os.write(self.master,b'\x1b')
            self.read_until(lambda: '受管理的 Codex 会话'.encode() in self.output)
        os.write(self.master, b'q\x03')
        self.read_until(lambda: '5 秒内再按一次 Ctrl+C'.encode() in self.output)
        self.assertIsNone(self.proc.poll())
        os.kill(self.worker_pid, 0)
        self.assertFalse((Path(self.config['state_dir'])/'STOP').exists())
        os.write(self.master, b'\x03')
        self.assert_closed()

    def test_color_theme_switch_and_compact_resize_keep_application_alive(self):
        self.launch([str(ROOT/'control.sh')],theme='dark')
        self.assertIn(b'48;5;234',self.output)
        os.write(self.master,b'c')
        self.read_until(lambda:'浅色主题'.encode() in self.output)
        self.assertIn(b'48;5;255',self.output)
        fcntl.ioctl(self.master,termios.TIOCSWINSZ,struct.pack('HHHH',18,70,0,0))
        self.proc.send_signal(signal.SIGWINCH)
        self.output.clear()
        os.write(self.master,b'?')
        self.read_until(lambda:'键盘帮助'.encode() in self.output)
        self.assertIsNone(self.proc.poll())
        self.proc.send_signal(signal.SIGTERM)
        self.assert_closed()

    def test_render_failure_keeps_worker_alive_and_retry_restores_interface(self):
        script = '''
from codex_watchdog.control import main
from codex_watchdog.renderer import Renderer
original = Renderer.draw
failed = False
def draw(self):
    global failed
    if not failed:
        failed = True
        raise TypeError('injected display failure')
    return original(self)
Renderer.draw = draw
main()
'''
        self.launch([sys.executable, '-c', script])
        self.read_until(lambda:'界面暂不可用'.encode() in self.output)
        self.assertIsNone(self.proc.poll())
        os.kill(self.worker_pid, 0)
        self.assertIn('injected display failure',
                      (Path(self.config['state_dir'])/'ui.log').read_text())
        self.assertFalse((Path(self.config['state_dir'])/'STOP').exists())
        self.output.clear()
        os.write(self.master, b'r')
        self.read_until(lambda:'受管理的 Codex 会话'.encode() in self.output)
        os.kill(self.worker_pid, 0)
        os.write(self.master, b'\x03')
        self.read_until(lambda:'5 秒内再按一次 Ctrl+C'.encode() in self.output)
        os.write(self.master, b'\x03')
        self.assert_closed()

    def test_npm_launcher_forwards_double_ctrl_c_and_resize(self):
        import shutil
        node=shutil.which('node')
        if node is None:
            self.skipTest('Node.js is not installed')
        self.launch([node,str(ROOT/'bin/codex-watchdog.cjs')])
        # The status PID belongs to the Python UI, its worker is owned by it.
        status=json.loads((Path(self.config['state_dir'])/'status.json').read_text())
        ui_pid=status['pid']
        fcntl.ioctl(self.master,termios.TIOCSWINSZ,struct.pack('HHHH',18,70,0,0))
        self.proc.send_signal(signal.SIGWINCH)
        self.output.clear()
        os.write(self.master,b'g')
        self.read_until(lambda:'监控规则与提醒设置'.encode() in self.output)
        os.write(self.master,b'\x03')
        self.read_until(lambda:'5 秒内再按一次 Ctrl+C'.encode() in self.output)
        self.assertIsNone(self.proc.poll())
        os.kill(ui_pid,0)
        os.kill(self.worker_pid,0)
        os.write(self.master,b'\x03')
        self.assert_closed()
        with self.assertRaises(ProcessLookupError):
            os.kill(ui_pid,0)

    def test_npm_launcher_disappearance_stops_owned_worker(self):
        import shutil
        node=shutil.which('node')
        if node is None:
            self.skipTest('Node.js is not installed')
        self.launch([node,str(ROOT/'bin/codex-watchdog.cjs')])
        self.proc.kill()
        self.read_until(lambda:(Path(self.config['state_dir'])/'STOP').exists(),timeout=5)
        def worker_stopped():
            try:
                os.kill(self.worker_pid,0)
            except ProcessLookupError:
                return True
            return False
        self.read_until(worker_stopped,timeout=5)
        service_status=Path(self.config['state_dir'])/'service-status.json'
        self.read_until(lambda:service_status.exists()
                        and json.loads(service_status.read_text()).get('phase')=='stopped',timeout=5)
        self.worker_pid=None

    def test_rules_enable_recovery_and_time_keys_do_not_switch_theme(self):
        config=load_config(self.path)
        config.update(observe_only=True,user_authorized_capacity_resume=False)
        self.path.write_text(json.dumps(config))
        self.launch([str(ROOT/'control.sh')],theme='dark')
        os.write(self.master,b'g')
        self.read_until(lambda:'selected model is at capacity'.encode() in self.output)
        os.write(self.master,b'm')
        self.read_until(lambda:'启用自动恢复'.encode() in self.output)
        self.assertTrue(load_config(self.path)['observe_only'])
        os.write(self.master,b'y')
        self.read_until(lambda:not load_config(self.path)['observe_only']
                        and '规则已更新'.encode() in self.output)
        self.assertEqual(load_config(self.path)['pattern_modes']['capacity'],'recover')
        self.output.clear()
        os.write(self.master,b'T')
        self.read_until(lambda:'输入分钟数'.encode() in self.output)
        self.assertNotIn('浅色主题'.encode(),self.output)
        os.write(self.master,b'\x1b')
        self.output.clear()
        os.write(self.master,b'C')
        self.read_until(lambda:'浅色主题'.encode() in self.output)
        self.proc.send_signal(signal.SIGTERM)
        self.assert_closed()

    def test_first_start_saves_setup_and_opens_application(self):
        self.path.unlink()
        env={**os.environ,'TERM':'xterm-256color','CODEX_WATCHDOG_CONFIG':str(self.path),
             'CODEX_WATCHDOG_PYTHON':sys.executable}
        self.master,slave=pty.openpty()
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,130,0,0))
        try:
            self.proc=subprocess.Popen([str(ROOT/'control.sh')],cwd=ROOT,env=env,
                                       stdin=slave,stdout=slave,stderr=slave,
                                       preexec_fn=controlling_terminal)
        finally:
            os.close(slave)
        self.read_until(lambda:'首次配置'.encode() in self.output)
        os.write(self.master,b'\n')
        status_path=self.path.parent/'state/status.json'
        self.read_until(lambda:status_path.exists())
        self.worker_pid=json.loads(status_path.read_text())['pid']
        self.assertTrue(load_config(self.path)['observe_only'])
        os.write(self.master,b'\x03')
        self.read_until(lambda:'5 秒内再按一次 Ctrl+C'.encode() in self.output)
        os.write(self.master,b'\x03')
        self.assert_closed()

    def test_run_script_termination_stops_worker(self):
        self.launch([str(ROOT/'run.sh'), 'start'])
        self.proc.send_signal(signal.SIGHUP)
        self.assert_closed()

    def test_start_without_a_terminal_leaves_no_worker(self):
        result = subprocess.run([str(ROOT/'control.sh'), '--config', str(self.path), 'start'],
                                env={**os.environ, 'TERM': 'xterm-256color',
                                     'CODEX_WATCHDOG_PYTHON': sys.executable},
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((Path(self.config['state_dir'])/'status.json').exists())

    def test_tui_entry_and_external_stop_close_both(self):
        self.launch([sys.executable, '-m', 'codex_watchdog.tui'])
        result = subprocess.run([str(ROOT/'control.sh'), '--config', str(self.path), 'stop'],
                                env={**os.environ, 'CODEX_WATCHDOG_PYTHON': sys.executable},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_closed()

    def test_duplicate_start_does_not_stop_existing_application(self):
        self.launch([str(ROOT/'control.sh'), 'start'])
        other = Service(self.path, self.config)
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            other.start()
        other.stop()
        self.assertIsNone(self.proc.poll())
        os.kill(self.worker_pid, 0)
        self.assertFalse((Path(self.config['state_dir'])/'STOP').exists())
        self.proc.send_signal(signal.SIGTERM)
        self.assert_closed()
