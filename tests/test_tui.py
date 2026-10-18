import curses
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codex_watchdog.tui import App
from codex_watchdog.view import dashboard, fit, cells, filter_rows, retry_text


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
        self.status()
        self.assertEqual(dashboard(self.path,110)['health'],'running')
        self.assertEqual(dashboard(self.path,130)['health'],'stale')
        (self.root/'state/STOP').touch()
        self.assertEqual(dashboard(self.path,110)['health'],'stopped')

    def test_corrupt_status_still_lists_offline_targets_for_removal(self):
        (self.root/'state/status.json').write_text('{')
        data = dashboard(self.path,100)
        self.assertEqual(data['rows'][0]['thread_id'],'one')
        self.assertIsNotNone(data['error'])

    def test_new_registration_does_not_display_old_recovery_state(self):
        self.status(targets={'one':{'registration_id':'v0','phase':'backoff'}})
        self.assertEqual(dashboard(self.path,110)['rows'][0]['phase'],'pending_observation')

    def test_add_key_discovers_choices_and_enter_adds_without_id_input(self):
        app = self.app()
        app.task = Mock()
        app.key('a')
        self.assertEqual(app.mode,'add')
        self.assertEqual(app.task.call_args.args[0],'discover')
        app.candidates = [{'thread_id':'two','cwd':'/p/Two','label':'Two','title':'analysis','runtime':'idle'}]
        app.rows = app.choose_rows()
        app.key('\n')
        self.assertEqual(app.task.call_args.args[0],'add')
        self.assertEqual(app.task.call_args.args[-1],'two')

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

    def test_quit_and_browsing_never_edit_config_or_call_codex(self):
        before = self.path.read_bytes()
        with patch('codex_watchdog.registry.Client',side_effect=AssertionError('unexpected RPC')):
            app = self.app()
            app.key('\n')
            app.key('q')
            app.key('l')
            app.key('q')
            app.key('q')
        self.assertTrue(app.quit)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse((self.root/'state/STOP').exists())

    def test_filter_and_unicode_clipping_work_for_chinese_titles(self):
        self.assertEqual(len(filter_rows([{'title':'论文分析','label':'A'}],'论文')),1)
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
