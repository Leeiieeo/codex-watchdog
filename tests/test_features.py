import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, HTTPServer

from codex_watchdog.diagnostics import doctor
from codex_watchdog.events import EventLog, read_events, read_metrics
from codex_watchdog.notifications import Notifier
from codex_watchdog.patterns import classify, safe_text
from codex_watchdog.registry import load_config, set_session_enabled, set_pattern_mode
from codex_watchdog.service import Service
from codex_watchdog.setup import Setup, default_config
from codex_watchdog.storage import StateError, load_state, save_state, rotate_file
from codex_watchdog.tui import App
from codex_watchdog.view import dashboard, cells
from codex_watchdog.watchdog import evaluate, attempt, record_observation
from test_watchdog import snap, failed, FakeServer


class Features(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'config.json'
        self.target = {'thread_id':'thread-1', 'cwd':'/project', 'label':'研究项目',
                       'registration_id':'registration-1'}
        self.path.write_text(json.dumps({'user_authorized_capacity_resume':True,
            'codex_home':str(self.root),'socket_path':str(self.root/'socket'),
            'state_dir':str(self.root/'state'),'targets':[self.target]}))
        self.config = load_config(self.path)
        Path(self.config['state_dir']).mkdir()
        self.state = {}
        evaluate(self.state, snap(), 100)

    def observe(self, snapshot, now, **kwargs):
        old = copy.deepcopy(self.state)
        due = evaluate(self.state, snapshot, now, **kwargs)
        record_observation(EventLog(self.config['state_dir']), old, self.state, snapshot, now)
        return due

    def test_connection_errors_are_recorded_but_never_recovered(self):
        error = failed(goal_status='active')
        error['turn']['error'] = {'codexErrorInfo':'responseStreamDisconnected', 'message':'connection failed'}
        for now in (111, 500, 2000):
            self.assertFalse(self.observe(error, now))
        self.assertEqual(self.state['reason_code'], 'observe_only')
        self.assertEqual(self.state['last_failure']['pattern'], 'connection')
        events = read_events(self.config['state_dir'])
        self.assertEqual(sum(e['kind']=='failure_detected' for e in events), 1)
        self.assertEqual(read_metrics(self.config['state_dir'])['thread-1']['failures'],1)
        self.assertFalse(classify({'codexErrorInfo':'unauthorized','message':'connection failed'}).recoverable)
        self.assertIsNone(classify({'message':'a user discussed connection failed'}))

    def test_unmatched_failure_retains_reason_and_redacted_evidence(self):
        error = failed(goal_status='active')
        error['turn']['error'] = {'codexErrorInfo':'unknown',
            'message':'request https://user:pass@example.com/path?token=private Bearer fake-secret'}
        self.observe(error,111)
        self.assertEqual(self.state['reason_code'],'unmatched_failure')
        evidence = (Path(self.config['state_dir'])/'events.jsonl').read_text()
        self.assertNotIn('private',evidence)
        self.assertNotIn('fake-secret',evidence)
        self.assertNotIn('user:pass',evidence)

    def test_guard_pause_and_resume_preserve_dedupe_and_baseline_failure(self):
        self.observe(failed(),111)
        self.state['attempted']=['old-attempt']
        paused = set_session_enabled(self.path,'thread-1',False,'registration-1')
        self.assertFalse(self.observe(failed(),120,target=paused))
        self.assertEqual(self.state['reason_code'],'guard_paused')
        self.assertIsNone(self.state['pending'])
        resumed = set_session_enabled(self.path,'thread-1',True,'registration-1')
        self.assertFalse(self.observe(failed(),200,target=resumed))
        self.assertEqual(self.state['reason_code'],'guard_resumed')
        self.assertIn('old-attempt',self.state['attempted'])
        self.assertFalse(self.observe(failed(),500,target=resumed))
        self.assertIsNone(self.state['pending'])
        self.observe(snap(turn_id='t2',started=501),501,target=resumed)
        self.observe(failed(turn_id='t2',started=501,completed=510),511,target=resumed)
        self.assertEqual(self.state['phase'],'backoff')

    def test_registry_stale_toggle_rejected_and_new_rules_cannot_recover(self):
        with self.assertRaises(ValueError):
            set_session_enabled(self.path,'thread-1',False,'old-registration')
        with self.assertRaises(ValueError):
            set_pattern_mode(self.path,'connection','recover')
        set_pattern_mode(self.path,'capacity','observe')
        config = load_config(self.path)
        self.observe(failed(goal_status='active'),111,config=config)
        self.assertIsNone(self.state['pending'])

    def test_global_observation_mode_cannot_schedule_capacity_resume(self):
        self.observe(failed(goal_status='active'),111,config={'observe_only':True})
        self.assertEqual(self.state['reason_code'],'observe_only')
        self.assertIsNone(self.state['pending'])
        self.assertFalse(evaluate(self.state,failed(goal_status='active'),500,config={'observe_only':True}))

    def test_recovery_timeline_tracks_running_completion_and_downtime(self):
        error = failed()
        self.observe(error,111)
        self.assertTrue(evaluate(self.state,error,171))
        events = EventLog(self.config['state_dir'])
        def persist():
            kind = {'resume_intent_saved':'resume_attempted','resume_accepted':'resume_accepted'}.get(self.state['phase'])
            if kind: events.record(kind,'thread-1',now=171)
        attempt(self.state,error,lambda:error,persist,lambda:{'goal':{'status':'active'}},lambda:False,171)
        self.observe(snap(turn_id='t2',started=172),180)
        self.observe(snap(turn_id='t2',started=172,turn_status='completed',runtime='idle',completed=190),191)
        self.observe(snap(turn_id='t2',started=172,turn_status='completed',runtime='idle',completed=190),200)
        kinds = [e['kind'] for e in read_events(self.config['state_dir'])]
        self.assertEqual(kinds,['failure_detected','recovery_scheduled','resume_attempted',
                                'resume_accepted','recovery_running','recovery_completed'])
        metrics=read_metrics(self.config['state_dir'])['thread-1']
        self.assertEqual(metrics['recoveries'],1)
        self.assertEqual(metrics['downtime_seconds'],70)

    def test_failed_followup_is_not_claimed_as_recovery(self):
        error=failed()
        self.observe(error,111)
        attempt(self.state,error,lambda:error,lambda:None,
                lambda:{'goal':{'status':'active'}},lambda:False,171)
        self.observe(failed(turn_id='t2',started=172,completed=180),181)
        self.assertNotIn('recovery_running',[e['kind'] for e in read_events(self.config['state_dir'])])

    def test_no_new_turn_and_stall_are_notifications_only(self):
        error=failed()
        self.observe(error,111)
        attempt(self.state,error,lambda:error,lambda:None,
                lambda:{'goal':{'status':'active'}},lambda:False,171)
        self.assertFalse(self.observe(error,500))
        self.assertTrue(self.state['recovery_unconfirmed'])
        self.state={}
        self.observe(snap(),100,config={'stall_after_seconds':60})
        self.assertFalse(self.observe(snap(),200,config={'stall_after_seconds':60}))
        self.assertTrue(self.state['stalled'])
        self.assertEqual(self.state['phase'],'running')

    def test_legacy_state_migration_preserves_attempts_and_pending(self):
        self.observe(failed(),111)
        self.state['attempted']=['old']
        state_path=Path(self.config['state_dir'])/'state.json'
        legacy=copy.deepcopy(self.state)
        legacy['pending']['fingerprint'][-1]=failed()['turn']['error']
        state_path.write_text(json.dumps({'thread-1':legacy}))
        migrated=load_state(state_path)
        self.assertEqual(migrated['thread-1']['attempted'],['old'])
        self.assertEqual(migrated['thread-1']['pending'],self.state['pending'])
        save_state(state_path,migrated)
        self.assertEqual(json.loads(state_path.read_text())['schema_version'],1)
        self.assertEqual(load_state(state_path),migrated)

    def test_metrics_rebuild_retained_events_after_corruption(self):
        events=EventLog(self.config['state_dir'])
        events.record('failure_detected','thread-1')
        (Path(self.config['state_dir'])/'metrics.json').write_text('[]')
        events.record('resume_attempted','thread-1')
        metrics=read_metrics(self.config['state_dir'])['thread-1']
        self.assertEqual(metrics['failures'],1)
        self.assertEqual(metrics['attempts'],1)
        self.assertTrue(list(Path(self.config['state_dir']).glob('metrics.json.corrupt-*')))

    def test_first_notification_after_empty_history_is_visible(self):
        app=App(str(self.path))
        app.refresh()
        EventLog(self.config['state_dir']).record('notification','thread-1',message='需要检查',terminal_bell=False)
        app.refresh()
        self.assertEqual(app.message,'需要检查')

    def test_corrupt_state_blocks_service_and_preserves_original(self):
        state_path=Path(self.config['state_dir'])/'state.json'
        state_path.write_text('{broken')
        service=Service(self.path,self.config)
        with patch.object(service,'_launch') as launch:
            with self.assertRaises(StateError): service.start()
            launch.assert_not_called()
        self.assertEqual(state_path.read_text(),'{broken')
        for raw in ({'schema_version':999,'targets':{}}, {'thread-1':{'attempted':[]}}):
            state_path.write_text(json.dumps(raw))
            with self.assertRaises(StateError): load_state(state_path)

    def test_health_distinguishes_live_service_and_failed_monitoring(self):
        status={'host':os.uname().nodename,'pid':os.getpid(),'checked_at':100,
                'targets':{'thread-1':{'phase':'suspended','error':'daemon disconnected',
                                      'last_success_at':80}}}
        root=Path(self.config['state_dir'])
        (root/'status.json').write_text(json.dumps(status))
        data=dashboard(self.path,now=101)
        self.assertEqual(data.service_health,'running')
        self.assertEqual(data.monitor_health,'error')
        self.assertEqual(data.successful_targets,0)
        self.assertEqual(data.last_success_age,21)

    def test_notifications_are_disabled_by_default_and_rate_limited(self):
        notifier=Notifier(self.config['state_dir'])
        notifier.notify({},'thread-1','attention','test',now=100)
        self.assertEqual(read_events(self.config['state_dir']),[])
        config={'notifications':{'enabled':True,'terminal_bell':True,'cooldown_seconds':300}}
        notifier.notify(config,'thread-1','attention','test',now=100)
        notifier.notify(config,'thread-1','attention','test',now=200)
        Notifier(self.config['state_dir']).notify(config,'thread-1','attention','test',now=300)
        self.assertEqual(len(read_events(self.config['state_dir'])),1)

    def test_webhook_posts_to_explicit_endpoint_without_logging_secret(self):
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
            def log_message(self,*args): pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.dict(os.environ,{'CODEX_WATCHDOG_TEST_WEBHOOK':f'http://127.0.0.1:{server.server_port}/private-secret'}):
            notifier=Notifier(self.config['state_dir'])
            notifier.notify({'notifications':{'enabled':True,'webhook_url_env':'CODEX_WATCHDOG_TEST_WEBHOOK'}},
                            'thread-1','attention','hello',now=100)
            notifier.queue.join()
        self.assertEqual(received[0]['message'],'hello')
        self.assertNotIn('private-secret',(Path(self.config['state_dir'])/'events.jsonl').read_text())

    def test_doctor_uses_only_read_rpc_and_reports_schema_errors(self):
        with closing(sqlite3.connect(self.root/'goals_1.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_goals(thread_id,goal_id,status,token_budget,tokens_used,created_at_ms,updated_at_ms)')
            db.execute("INSERT INTO thread_goals VALUES('thread-1','g1','active',NULL,10,100000,100000)")
        def handler(message):
            method=message['method']
            if method=='thread/loaded/list': return {'data':['thread-1']}
            if method=='thread/read': return {'thread':{'id':'thread-1','cwd':'/project','source':'cli','status':{'type':'active'}}}
            if method=='thread/goal/get': return {'goal':{'status':'active','tokenBudget':None,'tokensUsed':10}}
            if method=='thread/turns/list': return {'data':[snap()['turn']]}
            raise AssertionError('mutation '+method)
        server=FakeServer(str(self.root/'socket'),handler)
        try:
            result=doctor(self.path)
            self.assertTrue(result['ok'],result)
        finally:
            server.join()
        self.assertNotIn('thread/goal/set',[c['method'] for c in server.calls])

    def test_service_preserves_logs_and_stops_crash_loop(self):
        path=Path(self.config['state_dir'])/'service.log'
        path.write_text('earlier crash\n')
        service=Service(self.path,self.config)
        process=Mock(pid=123,returncode=1)
        process.poll.return_value=1
        with patch('codex_watchdog.service.subprocess.Popen',return_value=process):
            service._launch()
        service.output.close()
        self.assertIn('earlier crash',path.read_text())
        service.started_at=100
        with patch('codex_watchdog.service.time.monotonic',return_value=110):
            service.poll()
            service.retry_at=None
            service.poll()
            service.retry_at=None
            service.poll()
        self.assertTrue(service.blocked)
        self.assertEqual(json.loads((Path(self.config['state_dir'])/'service-status.json').read_text())['phase'],'blocked')
        rotate_file(path,max_bytes=1)
        self.assertIn('earlier crash',path.with_name('service.log.1').read_text())

    def test_setup_defaults_to_observation_and_does_not_overwrite(self):
        setup=Setup(self.root/'new-config.json')
        screen=Mock()
        screen.getmaxyx.return_value=(24,100)
        screen.get_wch.return_value='\n'
        self.assertTrue(setup.run(screen))
        config=load_config(setup.path)
        self.assertTrue(config['observe_only'])
        self.assertFalse(config['user_authorized_capacity_resume'])
        with self.assertRaises(ValueError): setup.run(screen)

    def test_config_location_handles_installed_package_and_override(self):
        with patch.dict(os.environ, {'CODEX_WATCHDOG_CONFIG':'/tmp/explicit.json'}):
            self.assertEqual(default_config(),'/tmp/explicit.json')
        with patch('codex_watchdog.setup.__file__',str(self.root/'site-packages/codex_watchdog/setup.py')), \
                patch.dict(os.environ, {'XDG_CONFIG_HOME':str(self.root/'config')}, clear=True):
            self.assertEqual(default_config(),str(self.root/'config/codex-watchdog/config.json'))

    def test_all_pages_fit_small_terminal_and_details_scroll(self):
        app=App(str(self.path))
        app.refresh()
        app.detail='thread-1'
        class Screen:
            def __init__(self): self.lines={}
            def getmaxyx(self): return (18,70)
            def erase(self): self.lines={}
            def refresh(self): pass
            def addstr(self,y,x,text,attr):
                self_outer.assertLess(cells(text)+x,70)
                self.lines.setdefault(y,[]).append(text)
        self_outer=self
        with patch('codex_watchdog.tui.curses.color_pair',return_value=0):
            for mode in ('main','add','detail','logs','events','patterns','doctor','help'):
                app.mode=mode
                app.scroll=0
                screen=Screen()
                app.draw(screen)
                self.assertIn('selected model is at capacity',''.join(text for line in screen.lines.values() for text in line))
                app.key('j')
                app.draw(screen)
        app.mode='main'
        app.rows=app.choose_rows()
        app.task=Mock()
        app.key('p')
        self.assertEqual(app.task.call_args.args[0],'toggle')


class DaemonFeatures(unittest.TestCase):
    setUp = Features.setUp
    def test_full_recovery_records_new_running_and_completed_turns(self):
        now = int(time.time())
        current = failed(goal_status='blocked', started=now-20, completed=now-10)
        current['runtime']='systemError'
        state={}
        evaluate(state,snap(started=now-20),now-20)
        evaluate(state,current,now-9)
        state['pending']['due']=time.time()-1
        state['_registration']='registration-1'
        save_state(Path(self.config['state_dir'])/'state.json',{'thread-1':state})
        self.config['check_interval_seconds']=1
        self.path.write_text(json.dumps(self.config))
        database=self.root/'goals_1.sqlite'
        with closing(sqlite3.connect(database)) as db,db:
            db.execute('CREATE TABLE thread_goals(thread_id,goal_id,status,token_budget,tokens_used,created_at_ms,updated_at_ms)')
            db.execute("INSERT INTO thread_goals VALUES('thread-1','g1','blocked',NULL,10,?,?)",
                       (now*1000-20000,current['goal']['updated_at_ms']))
        reads=0
        def handler(message):
            nonlocal reads
            method=message['method']
            if method=='thread/read':
                if current['runtime']=='active':
                    reads+=1
                    if reads>=2:
                        current.update(snap(turn_id='t2',started=now,turn_status='completed',
                                            completed=now+1,runtime='idle'))
                return {'thread':{'id':'thread-1','cwd':'/project','source':'cli','status':{'type':current['runtime']}}}
            if method=='thread/goal/get':
                return {'goal':{'status':current['goal']['status'],'tokenBudget':None,'tokensUsed':10}}
            if method=='thread/turns/list': return {'data':[current['turn']]}
            if method=='thread/goal/set':
                current.update(snap(turn_id='t2',started=now))
                with closing(sqlite3.connect(database)) as db,db:
                    db.execute("UPDATE thread_goals SET status='active',updated_at_ms=?",(now*1000,))
                return {'goal':{'status':'active'}}
            raise AssertionError('unexpected method '+method)
        server=FakeServer(str(self.root/'socket'),handler)
        proc=subprocess.Popen([sys.executable,'-m','codex_watchdog.watchdog','--config',str(self.path)],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if any(e['kind']=='recovery_completed' for e in read_events(self.config['state_dir'])): break
                time.sleep(.05)
            kinds=[e['kind'] for e in read_events(self.config['state_dir'])]
            self.assertIn('recovery_running',kinds)
            self.assertIn('recovery_completed',kinds)
            self.assertEqual(sum(c['method']=='thread/goal/set' for c in server.calls),1)
            self.assertEqual(read_metrics(self.config['state_dir'])['thread-1']['recoveries'],1)
        finally:
            (Path(self.config['state_dir'])/'STOP').touch()
            try: out,err=proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            server.join()

    def test_full_daemon_revalidates_guard_pause_before_write(self):
        now=int(time.time())
        current=failed(goal_status='blocked',started=now-20,completed=now-10)
        current['runtime']='systemError'
        state={}
        evaluate(state,snap(started=now-20),now-20)
        evaluate(state,current,now-9)
        state['pending']['due']=time.time()-1
        state['_registration']='registration-1'
        save_state(Path(self.config['state_dir'])/'state.json',{'thread-1':state})
        self.config['check_interval_seconds']=1
        self.path.write_text(json.dumps(self.config))
        with closing(sqlite3.connect(self.root/'goals_1.sqlite')) as db,db:
            db.execute('CREATE TABLE thread_goals(thread_id,goal_id,status,token_budget,tokens_used,created_at_ms,updated_at_ms)')
            db.execute("INSERT INTO thread_goals VALUES('thread-1','g1','blocked',NULL,10,?,?)",
                       (now*1000-20000,current['goal']['updated_at_ms']))
        reads=0
        paused=threading.Event()
        def handler(message):
            nonlocal reads
            method=message['method']
            if method=='thread/read':
                reads+=1
                if reads==2:
                    set_session_enabled(self.path,'thread-1',False,'registration-1')
                    paused.set()
                return {'thread':{'id':'thread-1','cwd':'/project','source':'cli','status':{'type':'systemError'}}}
            if method=='thread/goal/get': return {'goal':{'status':'blocked','tokenBudget':None,'tokensUsed':10}}
            if method=='thread/turns/list': return {'data':[current['turn']]}
            raise AssertionError('unexpected write '+method)
        server=FakeServer(str(self.root/'socket'),handler)
        proc=subprocess.Popen([sys.executable,'-m','codex_watchdog.watchdog','--config',str(self.path)],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.assertTrue(paused.wait(5))
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if load_state(Path(self.config['state_dir'])/'state.json')['thread-1'].get('phase')=='guard_paused': break
                time.sleep(.05)
            item=load_state(Path(self.config['state_dir'])/'state.json')['thread-1']
            self.assertEqual(item['phase'],'guard_paused')
            self.assertEqual(item['attempted'],[])
            self.assertNotIn('thread/goal/set',[c['method'] for c in server.calls])
        finally:
            (Path(self.config['state_dir'])/'STOP').touch()
            try: proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            server.join()
