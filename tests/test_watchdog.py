import base64
import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_watchdog.rpc import Client, RpcError
from codex_watchdog.watchdog import (atomic_json, attempt, capacity, evaluate, fingerprint,
                      goal_record, snapshot, configure, load_config)


def snap(turn_id="t1", turn_status="inProgress", runtime="active", goal_status="active",
         revision=100000, error=None, started=100, completed=None, goal_id="g1"):
    return {"thread_id": "thread-1", "runtime": runtime,
            "goal": {"goal_id": goal_id, "status": goal_status, "token_budget": None,
                     "tokens_used": 10, "created_at_ms": 100000, "updated_at_ms": revision},
            "turn": {"id": turn_id, "status": turn_status, "startedAt": started,
                     "completedAt": completed, "error": error}}


def failed(turn_id="t1", goal_status="paused", completed=110, **kwargs):
    return snap(turn_id=turn_id, turn_status="failed", runtime="idle", goal_status=goal_status,
                revision=completed*1000, completed=completed,
                error={"codexErrorInfo": "serverOverloaded", "message": "Selected model is at capacity. Please try a different model."},
                **kwargs)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
        evaluate(self.state, snap(), 100)

    def scheduled(self, s=None):
        s = s or failed()
        self.assertFalse(evaluate(self.state, s, 111))
        self.assertEqual(self.state["phase"], "backoff")
        return s

    def test_healthy_work_never_requests_recovery(self):
        for now in range(101, 1000):
            self.assertFalse(evaluate(self.state, snap(), now))
        self.assertEqual(self.state["attempted"], [])

    def test_old_capacity_error_at_start_is_baselined(self):
        state = {}
        for now in range(120, 500):
            self.assertFalse(evaluate(state, failed(), now))
        self.assertIsNone(state["pending"])

    def test_new_failure_waits_then_becomes_due(self):
        s = self.scheduled()
        self.assertFalse(evaluate(self.state, s, 170.99))
        self.assertTrue(evaluate(self.state, s, 171))

    def test_active_goal_capacity_failure_also_supported(self):
        self.scheduled(failed(goal_status="active"))

    def test_real_capacity_system_error_and_blocked_is_recovered(self):
        s = failed(goal_status="blocked")
        s["runtime"] = "systemError"
        s["goal"]["updated_at_ms"] += 31
        self.scheduled(s)
        self.assertTrue(evaluate(self.state, s, 171))
        mutation = Mock(return_value={"goal": {"status": "active"}})
        self.assertTrue(attempt(self.state, s, lambda: s, lambda: None,
                                mutation, lambda: False, 171))
        mutation.assert_called_once()
        for now in (172, 300, 1000):
            self.assertFalse(evaluate(self.state, s, now))

    def test_real_capacity_block_at_start_is_not_recovered(self):
        state = {}
        s = failed(goal_status="blocked")
        s["runtime"] = "systemError"
        for now in (120, 200, 1000):
            self.assertFalse(evaluate(state, s, now))
        self.assertIsNone(state["pending"])

    def test_manual_block_before_or_after_failure_is_inert(self):
        for revision in (109999, 111000, 120000):
            with self.subTest(revision=revision):
                state = {}
                evaluate(state, snap(), 100)
                s = failed(goal_status="blocked")
                s["runtime"] = "systemError"
                s["goal"]["updated_at_ms"] = revision
                for now in (111, 1000):
                    self.assertFalse(evaluate(state, s, now))
                self.assertIsNone(state["pending"])

    def test_system_error_with_other_failure_is_inert(self):
        s = failed(goal_status="blocked")
        s["runtime"] = "systemError"
        s["turn"]["error"]["codexErrorInfo"] = "internalServerError"
        evaluate(self.state, s, 111)
        self.assertIsNone(self.state["pending"])

    def test_user_pause_after_capacity_block_cancels_recovery(self):
        s = failed(goal_status="blocked")
        s["runtime"] = "systemError"
        self.scheduled(s)
        s["goal"].update(status="paused", updated_at_ms=120000)
        self.assertFalse(evaluate(self.state, s, 200))
        self.assertIsNone(self.state["pending"])

    def test_repeated_real_capacity_failures_continue_backoff(self):
        s = failed(goal_status="blocked")
        s["runtime"] = "systemError"
        self.scheduled(s)
        attempt(self.state, s, lambda: s, lambda: None,
                lambda: {"goal": {"status": "active"}}, lambda: False, 171)
        newer = failed(turn_id="t2", goal_status="blocked", started=172, completed=180)
        newer["runtime"] = "systemError"
        evaluate(self.state, newer, 181)
        self.assertEqual(self.state["pending"]["due"], 301)

    def test_jitter_never_shortens_backoff(self):
        evaluate(self.state, failed(), 111, jitter=0.1)
        self.assertEqual(self.state["pending"]["due"], 177)

    def test_manual_pause_without_error_not_resumed(self):
        s = snap(goal_status="paused", runtime="idle", turn_status="interrupted")
        self.assertFalse(evaluate(self.state, s, 120))
        self.assertFalse(self.state["armed"])

    def test_pause_after_failed_turn_is_not_resumed(self):
        s = failed()
        s["goal"]["updated_at_ms"] = 115000
        evaluate(self.state, s, 120)
        self.assertIsNone(self.state["pending"])

    def test_goal_edit_during_backoff_cancels(self):
        s = self.scheduled()
        s["goal"]["updated_at_ms"] += 1
        self.assertFalse(evaluate(self.state, s, 200))
        self.assertEqual(self.state["phase"], "cancelled")
        self.assertFalse(evaluate(self.state, s, 300))

    def test_goal_completion_cancels(self):
        s = self.scheduled()
        s["goal"]["status"] = "complete"
        self.assertFalse(evaluate(self.state, s, 200))
        self.assertIsNone(self.state["pending"])

    def test_all_terminal_goal_states_are_inert(self):
        for status in ["complete", "blocked", "usage_limited", "budget_limited", "unknown"]:
            with self.subTest(status=status):
                state = {}
                evaluate(state, snap(), 100)
                self.assertFalse(evaluate(state, failed(goal_status=status), 111))
                self.assertFalse(evaluate(state, failed(goal_status=status), 1000))

    def test_budget_exhausted_is_inert(self):
        s = failed()
        s["goal"]["token_budget"] = 10
        evaluate(self.state, s, 111)
        self.assertIsNone(self.state["pending"])

    def test_approval_or_running_state_cancels(self):
        s = self.scheduled()
        s["runtime"] = "active"
        self.assertFalse(evaluate(self.state, s, 200))
        self.assertIsNone(self.state["pending"])

    def test_new_user_turn_cancels_even_with_stale_runtime(self):
        self.scheduled()
        s = snap(turn_id="t2", runtime="idle", started=120)
        self.assertFalse(evaluate(self.state, s, 200))
        self.assertEqual(self.state["phase"], "running")

    def test_new_completed_turn_cancels(self):
        self.scheduled()
        s = snap(turn_id="t2", runtime="idle", turn_status="completed", completed=130)
        self.assertFalse(evaluate(self.state, s, 200))
        self.assertIsNone(self.state["pending"])

    def test_cleared_goal_cancels(self):
        s = self.scheduled()
        s["goal"] = None
        self.assertFalse(evaluate(self.state, s, 200))
        self.assertIsNone(self.state["pending"])

    def test_unloaded_or_unknown_runtime_is_inert(self):
        for status in ["notLoaded", "unknown", None]:
            self.state = {}
            evaluate(self.state, snap(), 100)
            s = failed()
            s["runtime"] = status
            self.assertFalse(evaluate(self.state, s, 111))
            self.assertIsNone(self.state["pending"])

    def test_noncapacity_errors_are_inert(self):
        for code in ["usageLimitExceeded", "rateLimitExceeded", "unauthorized", "internalServerError"]:
            s = failed()
            s["turn"]["error"]["codexErrorInfo"] = code
            self.assertFalse(capacity(s["turn"]["error"]))
            state = {}
            evaluate(state, snap(), 100)
            evaluate(state, s, 111)
            self.assertIsNone(state["pending"])

    def test_exact_capacity_message_fallback_only(self):
        self.assertTrue(capacity({"message": "Selected model is at capacity. Please try a different model."}))
        self.assertFalse(capacity({"message": "a user discussed selected model is at capacity"}))
        self.assertFalse(capacity("serverOverloaded"))

    def test_backoff_grows_and_caps(self):
        for attempts, delay in [(0, 60), (1, 120), (2, 240), (3, 300), (20, 300)]:
            state = {}
            evaluate(state, snap(), 100)
            state["attempts"] = attempts
            evaluate(state, failed(), 111)
            self.assertEqual(state["pending"]["due"], 111 + delay)

    def test_completed_work_resets_backoff(self):
        self.state["attempts"] = 5
        evaluate(self.state, snap(runtime="idle", turn_status="completed", completed=120), 121)
        self.assertEqual(self.state["attempts"], 0)

    def test_failure_before_start_after_rollback_is_inert(self):
        evaluate(self.state, failed(turn_id="old", started=90, completed=99), 111)
        self.assertIsNone(self.state["pending"])

    def test_new_goal_does_not_reuse_old_failure(self):
        s = failed(goal_id="new-goal")
        for now in [111, 200, 300]:
            self.assertFalse(evaluate(self.state, s, now))

    def test_revalidation_blocks_changed_runtime(self):
        s = self.scheduled()
        fresh = copy.deepcopy(s)
        fresh["runtime"] = "active"
        mutation = Mock()
        self.assertFalse(attempt(self.state, s, lambda: fresh, lambda: None, mutation, lambda: False, 171))
        mutation.assert_not_called()

    def test_revalidation_blocks_new_goal_revision(self):
        s = self.scheduled()
        fresh = copy.deepcopy(s)
        fresh["goal"]["updated_at_ms"] += 1
        mutation = Mock()
        attempt(self.state, s, lambda: fresh, lambda: None, mutation, lambda: False, 171)
        mutation.assert_not_called()

    def test_stop_sentinel_prevents_all_mutations(self):
        s = self.scheduled()
        read, mutation = Mock(), Mock()
        self.assertFalse(attempt(self.state, s, read, lambda: None, mutation, lambda: True, 171))
        read.assert_not_called()
        mutation.assert_not_called()

    def test_intent_persisted_before_single_resume(self):
        s = self.scheduled()
        writes = []
        def mutation():
            self.assertIn("t1", writes[-1]["attempted"])
            return {"goal": {"status": "active"}}
        self.assertTrue(attempt(self.state, s, lambda: s, lambda: writes.append(copy.deepcopy(self.state)),
                                mutation, lambda: False, 171))
        for now in [172, 300, 1000]:
            self.assertFalse(evaluate(self.state, s, now))
        self.assertEqual(self.state["attempts"], 1)

    def test_lost_ack_and_restart_do_not_repeat(self):
        s = self.scheduled()
        persisted = []
        def lost_ack():
            raise TimeoutError("ambiguous outcome")
        with self.assertRaises(TimeoutError):
            attempt(self.state, s, lambda: s, lambda: persisted.append(copy.deepcopy(self.state)),
                    lost_ack, lambda: False, 171)
        restarted = json.loads(json.dumps(persisted[-1]))
        for now in [200, 400, 1000]:
            self.assertFalse(evaluate(restarted, s, now))

    def test_new_capacity_failure_after_resume_retries(self):
        s = self.scheduled()
        attempt(self.state, s, lambda: s, lambda: None,
                lambda: {"goal": {"status": "active"}}, lambda: False, 171)
        # The new turn can fail between polls; we need not observe its active frame.
        next_failure = failed(turn_id="t2", started=172, completed=180)
        evaluate(self.state, next_failure, 181)
        self.assertEqual(self.state["pending"]["due"], 301)


class FakeServer:
    def __init__(self, path, handler, fragment=False, ping=False, invalid_accept=False):
        self.path, self.handler = path, handler
        self.fragment, self.ping, self.invalid_accept = fragment, ping, invalid_accept
        self.calls, self.errors = [], []
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(path)
        self.listener.listen()
        self.listener.settimeout(5)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @staticmethod
    def readn(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n-len(data))
            if not chunk:
                raise EOFError()
            data += chunk
        return data

    def frame(self, conn, data, opcode=1, final=True):
        if len(data) < 126:
            prefix = bytes([(128 if final else 0)|opcode, len(data)])
        else:
            prefix = bytes([(128 if final else 0)|opcode, 126]) + struct.pack("!H", len(data))
        # Deliberately split bytes to exercise partial reads.
        conn.sendall(prefix[:1])
        conn.sendall(prefix[1:] + data)

    def run(self):
        try:
            with self.listener.accept()[0] as conn:
                conn.settimeout(15)
                header = b""
                while not header.endswith(b"\r\n\r\n"):
                    header += self.readn(conn, 1)
                key = next(x.split(b":", 1)[1].strip() for x in header.split(b"\r\n")
                           if x.lower().startswith(b"sec-websocket-key:"))
                value = base64.b64encode(hashlib.sha1(key+b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                if self.invalid_accept:
                    value = b"wrong"
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "+value+b"\r\n\r\n")
                while True:
                    a, b = self.readn(conn, 2)
                    if not b & 128:
                        raise AssertionError("client frame not masked")
                    size = b & 127
                    if size == 126:
                        size = struct.unpack("!H", self.readn(conn, 2))[0]
                    elif size == 127:
                        size = struct.unpack("!Q", self.readn(conn, 8))[0]
                    mask = self.readn(conn, 4)
                    data = bytes(x ^ mask[i%4] for i,x in enumerate(self.readn(conn, size)))
                    if a & 15 == 10:
                        continue
                    message = json.loads(data)
                    self.calls.append(message)
                    if "id" not in message:
                        continue
                    result = {} if message["method"] == "initialize" else self.handler(message)
                    response = json.dumps({"id": message["id"], "result": result}).encode()
                    if self.ping:
                        self.frame(conn, b"ping", opcode=9)
                    if self.fragment:
                        middle = len(response)//2
                        self.frame(conn, response[:middle], final=False)
                        self.frame(conn, response[middle:], opcode=0)
                    else:
                        self.frame(conn, response)
        except (EOFError, BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            self.listener.close()

    def join(self):
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            raise AssertionError("fake server leaked")
        if self.errors:
            raise self.errors[0]


class IntegrationTests(unittest.TestCase):
    def test_unix_websocket_ping_fragmentation_and_long_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = FakeServer(tmp+"/s", lambda m: {"echo": m["params"]}, fragment=True, ping=True)
            client = Client(tmp+"/s")
            try:
                self.assertEqual(client.request("echo", {"text": "x"*1000}), {"echo": {"text": "x"*1000}})
            finally:
                client.close()
                server.join()

    def test_invalid_handshake_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = FakeServer(tmp+"/s", lambda _: {}, invalid_accept=True)
            with self.assertRaises(RpcError):
                Client(tmp+"/s")
            server.join()

    def test_real_rpc_snapshots_never_mutate_normal_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"goals_1.sqlite"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE thread_goals(thread_id,goal_id,status,token_budget,tokens_used,created_at_ms,updated_at_ms)")
                db.execute("INSERT INTO thread_goals VALUES('thread-1','g1','active',NULL,10,100000,100000)")
            def handler(m):
                if m["method"] == "thread/read":
                    return {"thread": {"id": "thread-1", "cwd": "/project", "status": {"type": "active"}, "source": "cli"}}
                if m["method"] == "thread/goal/get":
                    return {"goal": {"status": "active", "tokenBudget": None, "tokensUsed": 10}}
                if m["method"] == "thread/turns/list":
                    return {"data": [snap()["turn"]]}
                raise AssertionError("unexpected mutation: "+m["method"])
            server = FakeServer(tmp+"/s", handler)
            client = Client(tmp+"/s")
            try:
                state = {}
                for now in [100, 200, 1000]:
                    s = snapshot(client, {"thread_id": "thread-1", "cwd": "/project"}, tmp)
                    self.assertFalse(evaluate(state, s, now))
                self.assertEqual(goal_record(tmp, "thread-1")["status"], "active")
            finally:
                client.close()
                server.join()
            self.assertFalse(any(x["method"] == "thread/goal/set" for x in server.calls))

    def test_atomic_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"state.json"
            atomic_json(p, {"phase": "resume_intent_saved", "attempted": ["t1"]})
            self.assertEqual(json.loads(p.read_text())["attempted"], ["t1"])
            self.assertFalse(p.with_name("state.json.tmp").exists())

    def test_allowlist_add_remove_only_reads_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"config.json"
            config = {"user_authorized_capacity_resume": True, "codex_home": tmp,
                      "socket_path": tmp+"/s", "state_dir": tmp+"/state", "targets": []}
            atomic_json(path, config)
            client = Mock()
            client.request.return_value = {"thread": {"id": "thread-1", "cwd": "/project", "source": "cli"}}
            args = SimpleNamespace(config=str(path), add="thread-1", remove=None, label="Example")
            with patch("codex_watchdog.registry.Client", return_value=client):
                configure(args, config)
            client.request.assert_called_once_with("thread/read", {"threadId": "thread-1"})
            added = load_config(path)
            self.assertEqual(added["targets"][0]["thread_id"], "thread-1")
            self.assertTrue(added["targets"][0]["registration_id"])
            args.add, args.remove = None, "thread-1"
            configure(args, added)
            self.assertEqual(load_config(path)["targets"], [])

    def test_full_daemon_sends_one_resume_then_leaves_running_work_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, state_dir = Path(tmp)/"goals_1.sqlite", Path(tmp)/"state"
            state_dir.mkdir()
            current = failed(goal_status="blocked")
            current["runtime"] = "systemError"
            state = {}
            evaluate(state, snap(), 100)
            evaluate(state, current, 111)
            atomic_json(state_dir/"state.json", {"thread-1": state})
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE thread_goals(thread_id,goal_id,status,token_budget,tokens_used,created_at_ms,updated_at_ms)")
                db.execute("INSERT INTO thread_goals VALUES('thread-1','g1','blocked',NULL,10,100000,110000)")
            resumed, running_read = threading.Event(), threading.Event()
            def handler(m):
                method = m["method"]
                if method == "thread/read":
                    if resumed.is_set():
                        running_read.set()
                    return {"thread": {"id": "thread-1", "cwd": "/project", "status": {"type": current["runtime"]}, "source": "cli"}}
                if method == "thread/goal/get":
                    return {"goal": {"status": current["goal"]["status"], "tokenBudget": None, "tokensUsed": 10}}
                if method == "thread/turns/list":
                    return {"data": [current["turn"]]}
                if method == "thread/goal/set":
                    if m["params"] != {"threadId": "thread-1", "status": "active", "origin": "user"}:
                        raise AssertionError("unexpected resume payload")
                    current.update(snap(turn_id="t2", started=172))
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute("UPDATE thread_goals SET status='active',updated_at_ms=100000")
                    resumed.set()
                    return {"goal": {"status": "active"}}
                raise AssertionError("unexpected RPC: "+method)
            server = FakeServer(tmp+"/s", handler)
            config = {"user_authorized_capacity_resume": True, "codex_home": tmp,
                      "socket_path": tmp+"/s", "state_dir": str(state_dir),
                      "check_interval_seconds": 1,
                      "targets": [{"thread_id": "thread-1", "cwd": "/project", "label": "Fixture"}]}
            atomic_json(Path(tmp)/"config.json", config)
            proc = subprocess.Popen([sys.executable, "-m", "codex_watchdog.watchdog",
                                     "--config", tmp+"/config.json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(resumed.wait(4), "no recovery request")
                self.assertTrue(running_read.wait(8), "no follow-up read-only observation")
                (state_dir/"STOP").touch()
                out, err = proc.communicate(timeout=4)
                self.assertEqual(proc.returncode, 0, err.decode())
                writes = [m for m in server.calls if m["method"] == "thread/goal/set"]
                self.assertEqual(len(writes), 1)
                saved = json.loads((state_dir/"state.json").read_text())["thread-1"]
                self.assertEqual(saved["attempted"], ["t1"])
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                server.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
