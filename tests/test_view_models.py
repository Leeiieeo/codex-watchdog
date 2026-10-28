"""Exercise the worker JSON -> validated model -> renderer boundary."""
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_watchdog.presentation import goal_text, phase_text, severity
from codex_watchdog.tui import App
from codex_watchdog.view import dashboard
from codex_watchdog.view_models import GoalPresence, Observation, discovered_session, session_view
from test_presentation import Buffer


class ViewModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'config.json'
        self.target = {'thread_id':'one', 'cwd':'/project', 'label':'One', 'registration_id':'v1'}
        self.other = {'thread_id':'two', 'cwd':'/other', 'label':'Two'}
        self.config = {'observe_only':True, 'codex_home':str(self.root),
                       'socket_path':str(self.root/'socket'), 'state_dir':str(self.root),
                       'targets':[self.target, self.other]}
        self.path.write_text(json.dumps(self.config))

    def write_status(self, first, second=None, **header):
        data = {'host':os.uname().nodename, 'pid':os.getpid(), 'checked_at':100,
                'targets':{'one':first, 'two':second or {'runtime':'active', 'goal_status':'active', 'phase':'running'}}}
        data.update(header)
        (self.root/'status.json').write_text(json.dumps(data))

    def render(self, model):
        app = App(str(self.path))
        app.data = model
        screen = Buffer(45, 180)
        app.draw(screen)
        self.assertIsNone(app.render_error)
        main_text = screen.text
        for row in model.rows:
            app.mode, app.detail = 'detail', row.thread_id
            app.draw(screen)
            self.assertIsNone(app.render_error)
        return main_text

    def test_pending_absent_failed_and_stale_are_distinct(self):
        pending = session_view(self.target)
        absent = session_view(self.target, {'goal_status':None, 'runtime':'active', 'phase':'baseline'})
        failed = session_view(self.target, {'phase':'suspended', 'error':'disconnected'})
        stale = session_view(self.target, {'goal_status':None, 'phase':'baseline'}, stale=True)
        self.assertEqual((pending.observation, pending.goal.presence), (Observation.PENDING, GoalPresence.UNKNOWN))
        self.assertEqual((absent.observation, absent.goal.presence), (Observation.CURRENT, GoalPresence.ABSENT))
        self.assertEqual((failed.observation, failed.goal.presence), (Observation.FAILED, GoalPresence.UNKNOWN))
        self.assertEqual(stale.observation, Observation.STALE)
        self.assertEqual([goal_text(row) for row in (pending, absent, failed, stale)],
                         ['-', '未设置', '读取失败', '未设置（过期）'])
        self.assertEqual(phase_text(stale), '数据过期')
        with self.assertRaises(FrozenInstanceError):
            absent.runtime = 'idle'

    def test_new_enum_values_remain_visible_without_guessing_their_meaning(self):
        row = session_view(self.target, {'runtime':'futureRuntime', 'goal_status':'futureGoal', 'phase':'futurePhase'})
        self.assertEqual(row.goal.presence, GoalPresence.PRESENT)
        self.assertEqual(goal_text(row), 'futureGoal')
        self.assertEqual(phase_text(row), 'futurePhase')
        self.assertNotEqual(severity(row), 'success')

    def test_discovery_never_claims_to_have_read_a_goal(self):
        row = discovered_session({**self.target, 'runtime':'active', 'updated_at':100, 'goal_status':None})
        self.assertEqual(row.goal.presence, GoalPresence.UNKNOWN)
        self.assertEqual(row.observation, Observation.PENDING)

    def test_observations_cannot_replace_registration_identity(self):
        row = session_view(self.target, {'thread_id':'other', 'cwd':'/wrong', 'label':'Wrong',
                                        'enabled':False, 'goal_status':None, 'phase':'baseline'})
        self.assertEqual((row.thread_id, row.cwd, row.label, row.enabled), ('one', '/project', 'One', True))

    def test_reenrollment_and_guard_changes_discard_obsolete_snapshots(self):
        for binding in ({'registration_id':'old'}, {'guard_revision':'old'}):
            with self.subTest(binding=binding):
                row = session_view(self.target, {**binding, 'goal_status':None, 'phase':'baseline'})
                self.assertEqual(row.observation, Observation.PENDING)
                self.assertEqual(row.goal.presence, GoalPresence.UNKNOWN)

    def test_malformed_session_does_not_hide_healthy_session(self):
        invalid = [None, [], {'runtime':[]}, {'goal_status':{}}, {'last_failure':[]},
                   {'last_failure':{'message':{}}}, {'next_retry_at':'soon'},
                   {'next_retry_at':float('nan')}, {'attempts':True}, {'attention':'yes'}]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.write_status(raw)
                model = dashboard(self.path, now=101)
                self.assertEqual(model.rows[0].observation, Observation.FAILED)
                self.assertEqual(model.rows[1].observation, Observation.CURRENT)
                self.assertEqual(model.monitor_health, 'degraded')
                self.assertEqual(model.successful_targets, 1)
                rendered = self.render(model)
                self.assertIn('读取异常', rendered)
                self.assertIn('Two', rendered)

    def test_invalid_metrics_are_visible_without_crashing_or_modifying_history(self):
        self.write_status({'runtime':'active', 'goal_status':None, 'phase':'baseline'})
        for raw in ([], {'recoveries':'one'}, {'downtime_seconds':float('inf')}, {'attempts':-1}):
            with self.subTest(raw=raw):
                path = self.root/'metrics.json'
                path.write_text(json.dumps({'targets':{'one':raw}}))
                before = path.read_bytes()
                model = dashboard(self.path, now=101)
                self.assertEqual(model.rows[0].observation, Observation.FAILED)
                self.render(model)
                self.assertEqual(path.read_bytes(), before)

    def test_missing_snapshot_is_pending_and_corrupt_snapshot_is_failed(self):
        model = dashboard(self.path, now=100)
        self.assertEqual(model.rows[0].observation, Observation.PENDING)
        (self.root/'status.json').write_text('{')
        model = dashboard(self.path, now=100)
        self.assertEqual(model.rows[0].observation, Observation.FAILED)
        self.assertIsNotNone(model.error)
        self.render(model)

    def test_invalid_header_numbers_never_reach_countdown_or_age_rendering(self):
        for name, value in (('checked_at', 'yesterday'), ('checked_at', True), ('checked_at', float('inf')),
                            ('next_check_at', float('nan')), ('pid', []), ('targets', [])):
            with self.subTest(name=name, value=value):
                self.write_status({'phase':'running'}, check_interval_seconds=300, **{name:value})
                model = dashboard(self.path, now=101)
                self.assertIsNotNone(model.error)
                self.assertEqual(model.successful_targets, 0)
                self.render(model)

    def test_service_phase_null_or_invalid_does_not_break_header(self):
        for phase in (None, [], {}):
            with self.subTest(phase=phase):
                (self.root/'service-status.json').write_text(json.dumps({
                    'host':os.uname().nodename, 'pid':os.getpid(), 'phase':phase}))
                self.render(dashboard(self.path, now=101))

    def test_stopped_and_overdue_observations_are_marked_stale(self):
        self.write_status({'runtime':'active', 'goal_status':'active', 'phase':'running'})
        for now in (421, 101):
            if now == 101:
                (self.root/'STOP').touch()
            model = dashboard(self.path, now=now)
            self.assertEqual(model.rows[0].observation, Observation.STALE)
            self.assertEqual(model.successful_targets, 0)
            self.assertIn('数据过期', self.render(model))

    def test_cli_serialization_preserves_session_fields_and_excludes_config(self):
        self.write_status({'runtime':'active', 'goal_status':None, 'phase':'baseline'})
        data = dashboard(self.path, now=101).to_dict()
        json.dumps(data, allow_nan=False)
        self.assertNotIn('config', data)
        self.assertEqual(data['rows'][0]['thread_id'], 'one')
        self.assertIsNone(data['rows'][0]['goal_status'])
        self.assertEqual(data['rows'][0]['goal_presence'], 'absent')
        self.assertEqual(data['rows'][0]['observation'], 'current')
