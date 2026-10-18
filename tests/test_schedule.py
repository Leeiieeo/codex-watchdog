import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_watchdog.registry import load_config, set_check_interval
from codex_watchdog.schedule import wait_for_next_check


class Clock:
    def __init__(self, callback=None):
        self.elapsed = 0
        self.callback = callback

    def monotonic(self): return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds
        if self.callback: self.callback(self.elapsed)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'config.json'
        self.output = self.path.parent/'status.json'
        self.path.write_text(json.dumps({'user_authorized_capacity_resume':True,
            'codex_home':self.tmp.name,'socket_path':self.tmp.name+'/socket',
            'state_dir':self.tmp.name+'/state','targets':[]}))
        self.config = load_config(self.path)
        self.status = {'checked_at':100,'targets':{'one':{'next_retry_at':160}}}
        self.state = {'one':{'pending':{'due':160}}}

    def run_wait(self, clock, stopped=lambda:False):
        with patch('codex_watchdog.schedule.time',clock):
            wait_for_next_check(self.path,self.config,self.status,self.state,self.output,stopped)

    def test_retry_deadline_wakes_before_normal_five_minute_check(self):
        clock = Clock()
        self.run_wait(clock)
        self.assertEqual(clock.elapsed,60)
        saved = json.loads(self.output.read_text())
        self.assertEqual(saved['next_check_at'],160)
        self.assertEqual(saved['check_interval_seconds'],300)
        self.assertEqual(saved['targets']['one']['next_retry_at'],160)

    def test_shorter_interval_is_applied_without_restart(self):
        def update(elapsed):
            if elapsed==2: set_check_interval(self.path,10)
        clock = Clock(update)
        self.run_wait(clock)
        self.assertEqual(clock.elapsed,10)
        self.assertEqual(self.status['checked_at'],100)
        self.assertEqual(self.status['next_check_at'],110)

    def test_longer_interval_extends_the_current_wait(self):
        self.state = {}
        set_check_interval(self.path,5)
        self.config = load_config(self.path)
        def update(elapsed):
            if elapsed==2: set_check_interval(self.path,600)
        clock = Clock(update)
        self.run_wait(clock)
        self.assertEqual(clock.elapsed,600)

    def test_no_pending_retry_keeps_normal_interval(self):
        self.state = {}
        clock = Clock()
        self.run_wait(clock)
        self.assertEqual(clock.elapsed,300)

    def test_retry_after_next_check_is_displayed_at_its_own_deadline(self):
        self.state['one']['pending']['due'] = 430
        clock = Clock()
        self.run_wait(clock)
        self.assertEqual(clock.elapsed,300)
        self.assertEqual(self.status['targets']['one']['next_retry_at'],430)
        self.status['checked_at'] = 400
        clock = Clock()
        self.run_wait(clock)
        self.assertEqual(clock.elapsed,30)

    def test_failed_observation_waits_for_reconnection_without_spinning(self):
        self.status['targets']['one']['error'] = 'disconnected'
        self.state['one']['pending']['due'] = 90
        clock = Clock()
        self.run_wait(clock)
        self.assertEqual(clock.elapsed,30)
        self.assertIsNone(self.status['targets']['one']['next_retry_at'])

    def test_configuration_error_with_overdue_retry_does_not_spin(self):
        self.status['error']='configuration unavailable'
        self.state['one']['pending']['due']=90
        clock=Clock()
        self.run_wait(clock)
        self.assertEqual(clock.elapsed,30)

    def test_target_change_wakes_daemon_promptly(self):
        def update(elapsed):
            if elapsed==2:
                config = json.loads(self.path.read_text())
                config['targets']=[{'thread_id':'new','label':'New','cwd':'/project'}]
                self.path.write_text(json.dumps(config))
        clock = Clock(update)
        self.run_wait(clock)
        self.assertEqual(clock.elapsed,2)

    def test_stop_does_not_wait_five_minutes(self):
        clock = Clock()
        self.run_wait(clock,lambda:clock.elapsed>=2)
        self.assertEqual(clock.elapsed,2)
