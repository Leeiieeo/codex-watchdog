import curses
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codex_watchdog.tui import App
from codex_watchdog.view import dashboard, fit, cells, filter_rows, retry_text
from codex_watchdog.registry import load_config, set_pattern_mode
from codex_watchdog.theme import Palette
from codex_watchdog.watchdog import summary
from codex_watchdog.view_models import discovered_session, session_view
from test_presentation import Buffer


class TuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'config.json'
        self.target = {'thread_id':'one','cwd':'/project/SSpace','label':'SSpace','registration_id':'v1'}
        self.config = {'user_authorized_capacity_resume':True,'codex_home':str(self.root),
                       'socket_path':str(self.root/'socket'),'state_dir':str(self.root/'state'),
                       'targets':[self.target]}
        self.path.write_text(json.dumps(self.config))
        (self.root/'state').mkdir()

    def status(self, checked=100, **extra):
        data = {'checked_at':checked,'pid':os.getpid(),'host':os.uname().nodename,
                'targets':{'one':{'runtime':'active','goal_status':'active','phase':'running'}}}
        data.update(extra)
        (self.root/'state/status.json').write_text(json.dumps(data))

    def app(self):
        app = App(str(self.path))
        app.refresh()
        app.rows = app.choose_rows()
        return app

    def test_stale_heartbeat_never_appears_healthy(self):
        self.config['check_interval_seconds'] = 5
        self.path.write_text(json.dumps(self.config))
        self.status()
        self.assertEqual(dashboard(self.path,110).health,'running')
        self.assertEqual(dashboard(self.path,130).health,'stale')
        (self.root/'state/STOP').touch()
        self.assertEqual(dashboard(self.path,110).health,'stopped')

    def test_five_minute_wait_is_healthy_until_next_check_plus_grace(self):
        self.status()
        self.assertEqual(dashboard(self.path,399).health,'running')
        self.assertEqual(dashboard(self.path,421).health,'stale')
        self.assertEqual(dashboard(self.path,110).next_check_at,400)

    def test_corrupt_status_still_lists_offline_targets_for_removal(self):
        (self.root/'state/status.json').write_text('{')
        data = dashboard(self.path,100)
        self.assertEqual(data.rows[0].thread_id,'one')
        self.assertIsNotNone(data.error)

    def test_new_registration_does_not_display_old_recovery_state(self):
        self.status(targets={'one':{'registration_id':'v0','phase':'backoff'}})
        self.assertEqual(dashboard(self.path,110).rows[0].phase,'pending_observation')

    def test_add_key_discovers_choices_and_enter_adds_without_id_input(self):
        app = self.app()
        app.task = Mock()
        app.key('a')
        self.assertEqual(app.mode,'add')
        self.assertEqual(app.task.call_args.args[0],'discover')
        app.candidates = [discovered_session({'thread_id':'two','cwd':'/p/Two','label':'Two','title':'analysis','runtime':'idle'})]
        app.rows = app.choose_rows()
        app.key('\n')
        self.assertEqual(app.task.call_args.args[0],'add')
        self.assertEqual(app.task.call_args.args[-1],'two')

    def test_added_session_without_goal_survives_first_observation_and_details(self):
        self.config['targets'] = []
        self.path.write_text(json.dumps(self.config))
        app = self.app()
        # Complete real registry operations synchronously through the UI's queue.
        app.task = lambda kind, function, *args: app.results.put((kind, function(*args), None))
        client = Mock()
        thread = {'id':'one', 'cwd':self.target['cwd'], 'name':'No Goal session',
                  'source':'cli', 'status':{'type':'active'}}
        client.request.side_effect = lambda method, params: (
            {'data':['one']} if method == 'thread/loaded/list' else {'thread':thread})
        with patch('codex_watchdog.registry.Client', return_value=client):
            app.key('a')
            app.poll()
            app.draw(Buffer(30, 130))
            self.assertEqual(app.selected().thread_id, 'one')
            app.key('\n')
            app.poll()
        self.assertEqual(app.mode, 'main')
        target = load_config(self.path)['targets'][0]
        app.draw(Buffer(30, 130))  # Awaiting the worker's first observation.
        observed = summary({'runtime':'active', 'goal':None,
                            'turn':{'id':'turn-1', 'status':'inProgress'}})
        self.status(checked=time.time(), targets={'one':{**observed, 'registration_id':target['registration_id'],
                                  'phase':'baseline', 'reason_code':'no_goal',
                                  'reason':'该会话没有 Goal'}})
        app.refresh()
        screen = Buffer(30, 130)
        app.draw(screen)
        self.assertIn('运行中 · Goal 未设置', screen.text)
        self.assertIsNone(app.selected().goal.status)
        app.key('\n')
        screen = Buffer(45, 180)
        app.draw(screen)
        self.assertIn('运行中 / 未设置', screen.text)
        self.assertFalse(app.quit)
        self.assertEqual(load_config(self.path)['targets'], [target])
        self.assertTrue(all(call.args[0] in ('thread/loaded/list', 'thread/read')
                            for call in client.request.call_args_list))

    def test_remove_requires_confirmation_and_keeps_exact_registration(self):
        app = self.app()
        app.task = Mock()
        app.key('d')
        app.task.assert_not_called()
        app.key('n')
        app.task.assert_not_called()
        app.key('d')
        app.key('y')
        self.assertEqual(app.task.call_args.args[0],'remove')
        self.assertEqual(app.task.call_args.args[-2:],('one','v1'))

    def test_pause_key_has_the_same_action_in_both_cases(self):
        for key in ('p', 'P'):
            for enabled in (True, False):
                with self.subTest(key=key, enabled=enabled):
                    app = self.app()
                    app.rows[0] = replace(app.rows[0], enabled=enabled)
                    app.task = Mock()
                    app.key(key)
                    self.assertEqual(app.mode, 'main')
                    self.assertEqual(app.task.call_args.args[0], 'toggle')
                    self.assertEqual(app.task.call_args.args[-3:], ('one', not enabled, 'v1'))

    def test_rules_key_opens_rules_without_changing_guarding(self):
        before = self.path.read_bytes()
        for key in ('g', 'G'):
            with self.subTest(key=key):
                app = self.app()
                app.task = Mock()
                app.key(key)
                self.assertEqual(app.mode, 'patterns')
                app.task.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_observation_mode_requires_confirmation_before_enabling_recovery(self):
        self.config.update(observe_only=True, user_authorized_capacity_resume=False)
        self.path.write_text(json.dumps(self.config))
        before = self.path.read_bytes()
        app = self.app()
        app.key('g')
        app.task = Mock()
        app.key('m')
        self.assertEqual(app.recovery_confirmation, 'capacity')
        app.task.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        app.key('n')
        self.assertIsNone(app.recovery_confirmation)
        self.assertEqual(self.path.read_bytes(), before)
        app.key('m')
        app.key('y')
        app.task.call_args.args[1]()
        config = load_config(self.path)
        self.assertFalse(config['observe_only'])
        self.assertTrue(config['user_authorized_capacity_resume'])
        self.assertEqual(config['pattern_modes']['capacity'], 'recover')
        self.assertEqual(config['targets'], self.config['targets'])
        self.assertIn('policy_revision', config)

    def test_authorized_capacity_rule_switches_from_observe_to_recover(self):
        self.config['pattern_modes'] = {'capacity':'observe'}
        self.path.write_text(json.dumps(self.config))
        app = self.app()
        app.key('g')
        app.task = Mock()
        app.key('m')
        self.assertIsNone(app.recovery_confirmation)
        self.assertEqual(app.task.call_args.args[0], 'pattern')
        self.assertEqual(app.task.call_args.args[-2:], ('capacity','recover'))

    def test_registry_rejects_unconfirmed_exit_from_observation_mode(self):
        self.config.update(observe_only=True, user_authorized_capacity_resume=False)
        self.path.write_text(json.dumps(self.config))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            set_pattern_mode(self.path, 'capacity', 'recover')
        self.assertEqual(self.path.read_bytes(), before)

    def test_time_keys_open_the_same_dialog_without_switching_theme(self):
        for key in ('t', 'T'):
            with self.subTest(key=key):
                app = self.app()
                app.key('g')
                palette = app.palette
                app.key(key)
                self.assertEqual(app.interval_kind, 'stall')
                self.assertEqual(app.interval_input, '')
                self.assertIs(app.palette, palette)

    def test_color_keys_switch_theme_without_opening_time_dialog(self):
        for key in ('c', 'C'):
            with self.subTest(key=key), patch.dict(os.environ, {}, clear=True):
                app = self.app()
                app.palette = Palette('dark')
                app.key('g')
                app.key(key)
                self.assertEqual(app.palette.name, 'light')
                self.assertIsNone(app.interval_input)

    def test_theme_keys_do_not_intercept_filter_or_confirmation_input(self):
        app = self.app()
        app.key('/')
        palette = app.palette
        app.key('C')
        self.assertEqual(app.query, 'C')
        self.assertIs(app.palette, palette)
        app.key('\x1b')
        app.recovery_confirmation = 'capacity'
        app.key('C')
        self.assertIs(app.palette, palette)
        self.assertEqual(app.recovery_confirmation, 'capacity')

    def test_browsing_and_q_never_exit_or_edit_config_or_call_codex(self):
        before = self.path.read_bytes()
        with patch('codex_watchdog.registry.Client',side_effect=AssertionError('unexpected RPC')):
            app = self.app()
            app.key('\n')
            app.key('q')
            self.assertEqual(app.mode,'detail')
            app.key('\x1b')
            app.key('l')
            app.key('q')
            self.assertEqual(app.mode,'logs')
            app.key('\x1b')
            app.key('q')
            app.key('\x1b')
        self.assertFalse(app.quit)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse((self.root/'state/STOP').exists())

    def test_ctrl_c_twice_exits_from_every_screen_and_dialog(self):
        for mode in ('main','add','detail','logs','filter','interval','confirm'):
            with self.subTest(mode=mode):
                app = self.app()
                app.mode = mode if mode in ('main','add','detail','logs') else 'main'
                app.editing_filter = mode == 'filter'
                app.interval_input = '' if mode == 'interval' else None
                app.confirm = session_view(self.target) if mode == 'confirm' else None
                app.key('\x03')
                self.assertFalse(app.quit)
                self.assertGreater(app.exit_confirmation_until,time.monotonic())
                app.key('\x03')
                self.assertTrue(app.quit)

    def test_ctrl_c_confirmation_expires_after_five_seconds(self):
        app = self.app()
        with patch('codex_watchdog.tui.time.monotonic',return_value=100):
            app.interrupt()
        with patch('codex_watchdog.tui.time.monotonic',return_value=106):
            app.interrupt()
        self.assertFalse(app.quit)
        with patch('codex_watchdog.tui.time.monotonic',return_value=107):
            app.interrupt()
        self.assertTrue(app.quit)

    def test_filter_and_unicode_clipping_work_for_chinese_titles(self):
        self.assertEqual(len(filter_rows([session_view({'thread_id':'test','cwd':'/project','title':'论文分析','label':'A'})],'论文')),1)
        self.assertEqual(cells(fit('研究ab',5)),5)
        self.assertNotIn('\x1b',fit('a\x1b[31m',8))
        self.assertEqual(retry_text(161,100),'01:01')

    def test_error_from_background_discovery_is_visible_and_retryable(self):
        app = self.app()
        app.mode,app.busy = 'add','discover'
        app.results.put(('discover',None,'daemon unavailable'))
        app.poll()
        self.assertIsNone(app.busy)
        self.assertIn('daemon unavailable',app.message)
        app.task = Mock()
        app.key('r')
        self.assertEqual(app.task.call_args.args[0],'discover')

    def test_interval_can_be_entered_in_minutes_from_the_main_screen(self):
        app = self.app()
        app.task = Mock()
        app.key('i')
        for char in '10': app.key(char)
        app.key('\n')
        self.assertEqual(app.task.call_args.args[0],'interval')
        self.assertEqual(app.task.call_args.args[-1],600)
        self.assertIsNone(app.interval_input)

    def test_invalid_or_cancelled_interval_does_not_change_settings(self):
        before = self.path.read_bytes()
        app = self.app()
        app.task = Mock()
        app.key('i')
        app.key('0')
        app.key('\n')
        self.assertIsNotNone(app.interval_error)
        app.task.assert_not_called()
        app.key('\x1b')
        self.assertIsNone(app.interval_input)
        self.assertEqual(self.path.read_bytes(),before)
