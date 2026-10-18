import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from codex_watchdog.registry import add_session, discover_sessions, load_config, remove_session
from codex_watchdog.rpc import RpcError


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'config.json'
        self.config = {'user_authorized_capacity_resume': True, 'codex_home': self.tmp.name,
                       'socket_path': self.tmp.name+'/s', 'state_dir': self.tmp.name+'/state', 'targets': []}
        self.path.write_text(json.dumps(self.config))

    def client(self, tid='one'):
        c = Mock()
        c.request.return_value = {'thread': {'id': tid, 'cwd': '/project/'+tid,
                                           'name': 'Session '+tid, 'source': 'cli',
                                           'status': {'type': 'idle'}}}
        return c

    def test_add_only_reads_codex_and_duplicate_preserves_baseline(self):
        c = self.client()
        with patch('codex_watchdog.registry.Client', return_value=c):
            first = add_session(self.path, 'one')
            again = add_session(self.path, 'one')
        self.assertEqual(first['registration_id'], again['registration_id'])
        self.assertEqual(len(load_config(self.path)['targets']), 1)
        self.assertEqual({call.args[0] for call in c.request.call_args_list}, {'thread/read'})

    def test_discovery_lists_loaded_main_threads_including_idle_without_writes(self):
        c = self.client()
        def request(method, params):
            if method == 'thread/loaded/list':
                return {'data': ['one', 'two', 'child', 'closed']}
            if method == 'thread/read':
                tid = params['threadId']
                return {'thread': {'id': tid, 'cwd': '/project/'+tid, 'name': tid,
                    'parentThreadId': 'one' if tid == 'child' else None,
                    'status': {'type': 'notLoaded' if tid == 'closed' else 'idle'}, 'source': 'cli'}}
            raise AssertionError('mutation: '+method)
        c.request.side_effect = request
        with patch('codex_watchdog.registry.Client', return_value=c):
            result = discover_sessions(self.path)
        self.assertEqual([t['thread_id'] for t in result['sessions']], ['one', 'two'])
        self.assertEqual(result['warnings'], [])
        self.assertEqual(load_config(self.path)['targets'], [])

    def test_child_cannot_be_enrolled(self):
        c = self.client()
        c.request.return_value['thread']['source'] = {'subAgent': {'thread_spawn': {}}}
        with patch('codex_watchdog.registry.Client', return_value=c):
            with self.assertRaises(ValueError):
                add_session(self.path, 'one')
        self.assertEqual(load_config(self.path)['targets'], [])

    def test_empty_drafts_are_hidden_but_unnamed_real_sessions_remain(self):
        c = Mock()
        def request(method, params):
            if method == 'thread/loaded/list': return {'data': ['draft','empty','real']}
            if method == 'thread/read':
                return {'thread': {'id':params['threadId'],'cwd':'/project/one',
                                  'name':None,'status':{'type':'idle'},'source':'cli'}}
            if method == 'thread/turns/list':
                if params['threadId'] == 'draft':
                    raise RpcError('thread is not materialized yet')
                return {'data':[] if params['threadId']=='empty' else [{'id':'turn-one'}]}
            raise AssertionError('unexpected RPC')
        c.request.side_effect = request
        with patch('codex_watchdog.registry.Client',return_value=c):
            result = discover_sessions(self.path)
        self.assertEqual([t['thread_id'] for t in result['sessions']],['real'])
        self.assertEqual(result['warnings'],[])

    def test_concurrent_additions_are_not_lost(self):
        barrier = threading.Barrier(2)
        def factory(*_):
            c = Mock()
            def read(method, params):
                barrier.wait(timeout=3)
                tid = params['threadId']
                return {'thread': {'id': tid, 'cwd': '/project/'+tid, 'source': 'cli'}}
            c.request.side_effect = read
            return c
        errors = []
        def run(tid):
            try:
                add_session(self.path, tid)
            except Exception as exc:
                errors.append(exc)
        with patch('codex_watchdog.registry.Client', side_effect=factory):
            workers = [threading.Thread(target=run, args=(tid,)) for tid in ('one','two')]
            for worker in workers: worker.start()
            for worker in workers: worker.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual({t['thread_id'] for t in load_config(self.path)['targets']}, {'one','two'})

    def test_offline_remove_needs_no_connection_and_readd_gets_new_baseline(self):
        with patch('codex_watchdog.registry.Client', return_value=self.client()):
            old = add_session(self.path, 'one')
        with patch('codex_watchdog.registry.Client', side_effect=AssertionError('offline')):
            self.assertTrue(remove_session(self.path, 'one', old['registration_id']))
        with patch('codex_watchdog.registry.Client', return_value=self.client()):
            new = add_session(self.path, 'one')
        self.assertNotEqual(old['registration_id'], new['registration_id'])
        with self.assertRaises(ValueError):
            remove_session(self.path, 'one', old['registration_id'])
        self.assertEqual(load_config(self.path)['targets'], [new])

    def test_partial_discovery_failure_keeps_other_sessions_and_warning(self):
        c = self.client()
        def request(method, params):
            if method == 'thread/loaded/list': return {'data': ['gone','one']}
            if params['threadId'] == 'gone': raise RuntimeError('thread unloaded')
            return self.client().request.return_value
        c.request.side_effect = request
        with patch('codex_watchdog.registry.Client', return_value=c):
            result = discover_sessions(self.path)
        self.assertEqual(len(result['sessions']), 1)
        self.assertEqual(len(result['warnings']), 1)

    def test_relative_paths_are_based_on_config_directory(self):
        self.config['state_dir'] = 'state'
        self.path.write_text(json.dumps(self.config))
        self.assertEqual(load_config(self.path)['state_dir'], str((self.path.parent/'state').resolve()))

    def test_symlink_config_keeps_runtime_file_and_link_intact(self):
        link = self.path.parent/'linked.json'
        link.symlink_to(self.path)
        with patch('codex_watchdog.registry.Client', return_value=self.client()):
            target = add_session(link, 'one')
        self.assertTrue(link.is_symlink())
        self.assertEqual(load_config(self.path)['targets'], [target])
        remove_session(link, 'one')
        self.assertTrue(link.is_symlink())
        self.assertEqual(load_config(self.path)['targets'], [])
