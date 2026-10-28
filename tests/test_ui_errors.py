import curses
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codex_watchdog.renderer import Renderer
from codex_watchdog.tui import App
from codex_watchdog.view_models import Observation
from test_presentation import Buffer


class UiErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'config.json'
        self.config = {'observe_only':True, 'codex_home':str(self.root),
                       'socket_path':str(self.root/'socket'), 'state_dir':str(self.root),
                       'targets':[{'thread_id':'one', 'cwd':'/project', 'label':'One'}]}
        self.path.write_text(json.dumps(self.config))
        (self.root/'status.json').write_text(json.dumps({
            'host':os.uname().nodename, 'pid':os.getpid(), 'checked_at':time.time(),
            'targets':{'one':{'runtime':'active', 'goal_status':None, 'phase':'baseline'}}}))
        self.app = App(str(self.path))
        self.app.refresh()
        self.screen = Buffer(30, 130)

    def test_render_failure_has_logged_fallback_and_can_retry(self):
        with patch.object(Renderer, 'draw', side_effect=TypeError('broken display')):
            self.app.draw(self.screen)
        self.assertIn('界面暂不可用', self.screen.text)
        self.assertIn('r 重试', self.screen.text)
        self.assertFalse(self.app.quit)
        evidence = (self.root/'ui.log').read_text()
        self.assertIn('TypeError: broken display', evidence)
        self.assertIn('Traceback', evidence)
        self.app.key('r')
        self.app.draw(self.screen)
        self.assertIsNone(self.app.render_error)
        self.assertIn('Goal 未设置', self.screen.text)

    def test_fallback_blocks_hidden_confirmations_and_keeps_exit_controls(self):
        before = self.path.read_bytes()
        self.app.confirm = self.app.data.rows[0]
        self.app.recovery_confirmation = 'capacity'
        self.app.task = Mock()
        with patch.object(Renderer, 'draw', side_effect=TypeError('broken display')):
            self.app.draw(self.screen)
        for key in ('y', 'p', 'd', '\n'):
            self.app.key(key)
        self.app.task.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.app.key('\x03')
        self.app.draw(self.screen)
        self.assertIn('5 秒内再按 Ctrl+C', self.screen.text)
        self.app.key('\x03')
        self.assertTrue(self.app.quit)

    def test_fallback_can_open_ui_log_and_clear_hidden_dialogs(self):
        self.app.confirm = self.app.data.rows[0]
        self.app.interval_input = '5'
        self.app.recovery_confirmation = 'capacity'
        with patch.object(Renderer, 'draw', side_effect=TypeError('broken display')):
            self.app.draw(self.screen)
        self.app.key('l')
        self.assertEqual(self.app.log_source, 'ui')
        self.assertIsNone(self.app.confirm)
        self.assertIsNone(self.app.interval_input)
        self.assertIsNone(self.app.recovery_confirmation)
        self.app.draw(self.screen)
        self.assertIn('界面日志', self.screen.text)
        self.assertIn('broken display', self.screen.text)
        self.app.key('s')
        self.assertEqual(self.app.log_source, 'watchdog')

    def test_repeated_failures_are_throttled_and_secrets_redacted(self):
        exc = ValueError('Bearer private-token https://user:pass@example.com/path?secret=private-query')
        for _ in range(3):
            with patch.object(Renderer, 'draw', side_effect=exc):
                self.app.draw(self.screen)
            self.app.key('r')
        evidence = (self.root/'ui.log').read_text()
        self.assertEqual(evidence.count('] render main'), 1)
        for value in ('private-token', 'user:pass', 'private-query'):
            self.assertNotIn(value, evidence)

    def test_log_storage_failure_does_not_break_fallback(self):
        with patch.object(Renderer, 'draw', side_effect=TypeError('broken display')), \
                patch('codex_watchdog.ui_errors.rotate_file', side_effect=OSError('disk unavailable')):
            self.app.draw(self.screen)
        self.assertIn('界面暂不可用', self.screen.text)
        self.assertIn('日志写入失败', self.screen.text)

    def test_invalid_config_marks_cached_observation_stale_then_recovers(self):
        self.assertEqual(self.app.data.rows[0].observation, Observation.CURRENT)
        self.path.write_text('{')
        self.app.refresh()
        self.assertEqual(self.app.data.rows[0].observation, Observation.STALE)
        self.assertEqual(self.app.data.successful_targets, 0)
        self.assertEqual(self.app.data.monitor_health, 'error')
        self.app.draw(self.screen)
        self.assertIn('数据过期', self.screen.text)
        self.path.write_text(json.dumps(self.config))
        self.app.refresh()
        self.assertIsNone(self.app.view_error)
        self.assertEqual(self.app.data.rows[0].observation, Observation.CURRENT)

    def test_render_failure_does_not_interrupt_service_polling(self):
        service = Mock()
        service.stop_requested = False
        service.poll.return_value = None
        self.app.service = service
        self.screen.keypad = self.screen.timeout = self.screen.bkgd = lambda *_: None
        keys = iter(('r', '\x03', '\x03'))
        def input_key():
            if service.poll.call_count == 1:
                self.assertIn('界面暂不可用', self.screen.text)
            else:
                self.assertIsNone(self.app.render_error)
            return next(keys)
        self.screen.get_wch = input_key
        original = Renderer.draw
        calls = []
        def draw(renderer):
            calls.append(1)
            if len(calls) == 1:
                raise TypeError('broken display')
            original(renderer)
        with patch.object(Renderer, 'draw', draw), \
                patch('codex_watchdog.theme.curses.has_colors', return_value=False):
            self.app.run(self.screen)
        self.assertGreaterEqual(service.poll.call_count, 3)
        service.stop.assert_not_called()
        self.assertTrue(self.app.quit)
