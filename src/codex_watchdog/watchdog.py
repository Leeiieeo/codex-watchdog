#!/usr/bin/env python3
"""Recover new capacity failures; all normal observations are read-only."""
import argparse
import copy
from contextlib import closing
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import signal
import sqlite3
import time

from .rpc import Client
from .storage import atomic_json, load_state, save_state
from .registry import load_config, add_session, remove_session, discover_sessions
from .schedule import wait_for_next_check
from .patterns import CAPACITY_ERROR_CODE, CAPACITY_MESSAGE, classify, pattern_mode, safe_text, error_fingerprint
from .decisions import decision
from .events import EventLog
from .notifications import Notifier

LOG = logging.getLogger("codex-watchdog")
TERMINAL = {"complete", "usage_limited", "budget_limited"}
STATUS_MAP = {"usageLimited": "usage_limited", "budgetLimited": "budget_limited"}


def capacity(error):
    pattern = classify(error)
    return pattern is not None and pattern.id == 'capacity'


def goal_record(codex_home, thread_id):
    database = Path(codex_home) / "goals_1.sqlite"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT goal_id,status,token_budget,tokens_used,created_at_ms,updated_at_ms "
            "FROM thread_goals WHERE thread_id=?", (thread_id,)
        ).fetchone()
        return dict(row) if row else None


def snapshot(client, target, codex_home):
    tid = target["thread_id"]
    before = goal_record(codex_home, tid)
    thread = client.request("thread/read", {"threadId": tid})["thread"]
    if thread.get("id") != tid or thread.get("cwd") != target["cwd"]:
        raise ValueError("thread binding changed")
    source = thread.get("source")
    if isinstance(source, dict) and "subAgent" in source:
        raise ValueError("refusing to guard a child agent")
    goal = client.request("thread/goal/get", {"threadId": tid})["goal"]
    turns = client.request("thread/turns/list", {
        "threadId": tid, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"
    })["data"]
    after = goal_record(codex_home, tid)
    if before != after:
        raise ValueError("goal changed during observation")
    if bool(goal) != bool(after):
        raise ValueError("goal API/database mismatch")
    if goal and (STATUS_MAP.get(goal["status"], goal["status"]) != after["status"]
                 or goal.get("tokenBudget") != after["token_budget"]
                 or goal.get("tokensUsed") != after["tokens_used"]):
        raise ValueError("goal API/database revision mismatch")
    return {
        "thread_id": tid,
        "title": thread.get("name"),
        "updated_at": thread.get("updatedAt"),
        "runtime": thread.get("status", {}).get("type"),
        "goal": after,
        "turn": turns[0] if turns else None,
    }


def turn_key(s):
    t = s.get("turn") or {}
    return [t.get("id"), t.get("status"), t.get("completedAt")]


def fingerprint(s):
    g, t = s.get("goal") or {}, s.get("turn") or {}
    return [g.get("goal_id"), g.get("updated_at_ms"), g.get("status"),
            t.get("id"), t.get("completedAt"), error_fingerprint(t.get('error'))]


def eligible(s):
    g, t = s.get("goal") or {}, s.get("turn") or {}
    budget = g.get("token_budget")
    completed = t.get("completedAt")
    # Codex 0.162.1 ends capacity failures as systemError + blocked.
    # A generic block is NOT recoverable: its revision must coincide with
    # the final capacity error, and evaluate() must have observed active work.
    failure_block = (g.get("status") == "blocked"
                     and s.get("runtime") == "systemError"
                     and isinstance(completed, (int, float))
                     and isinstance(g.get("updated_at_ms"), (int, float))
                     and completed * 1000 <= g["updated_at_ms"] < (completed + 1) * 1000)
    return (s.get("runtime") in ("idle", "systemError")
            and (g.get("status") in ("active", "paused") or failure_block)
            and (budget is None or g.get("tokens_used", budget) < budget)
            and t.get("status") == "failed"
            and isinstance(t.get("completedAt"), (int, float))
            and capacity(t.get("error")))


def _evaluate(state, s, now, jitter=0.0, config=None):
    """Mutate only watchdog state. Return True only for a due recovery candidate."""
    g, t = s.get("goal") or {}, s.get("turn") or {}
    key = turn_key(s)
    if not state or state.get("goal_id") != g.get("goal_id"):
        state.clear()
        state.update(goal_id=g.get("goal_id"), last_turn=key,
                     armed=g.get("status") == "active", pending=None,
                     attempts=0, attempted=[], phase="baseline", first_seen=now,
                     baseline_turn=key,
                     newest_started_at=t.get("startedAt") or 0)
        decision(state, 'baseline', 'baseline' if g else 'no_goal')
        return False
    new_turn = state["last_turn"] != key
    previous_started = state.get("newest_started_at", 0)
    state["newest_started_at"] = max(previous_started, t.get("startedAt") or 0)
    state["last_turn"] = key
    if s.get("runtime") not in ("idle", "active", "systemError"):
        decision(state, 'unavailable', 'unavailable', pending=None, armed=False)
        return False
    if (not g or g.get("status") in TERMINAL
            or (g.get("status") not in ("active", "paused") and not eligible(s))):
        reason = ('no_goal' if not g else 'goal_complete' if g.get('status') == 'complete'
                  else 'goal_limited' if g.get('status') in TERMINAL else 'blocked')
        decision(state, 'goal_stopped', reason, pending=None, armed=False, attempts=0)
        return False
    if s["runtime"] == "active" or t.get("status") == "inProgress":
        decision(state, 'running', 'running', pending=None, armed=g['status'] == 'active')
        return False
    budget = g.get('token_budget')
    if budget is not None and g.get('tokens_used', budget) >= budget:
        decision(state, 'goal_stopped', 'budget_exhausted', pending=None, armed=False)
        return False
    if t.get('status') == 'failed':
        pattern = classify(t.get('error'))
        mode = pattern_mode(config or {}, pattern)
        if pattern is None or mode != 'recover':
            reason = ('unmatched_failure' if pattern is None else
                      'pattern_disabled' if mode == 'off' else 'observe_only')
            decision(state, 'observed_failure', reason, pending=None,
                     armed=g['status'] == 'active')
            return False
    pending = state.get("pending")
    if pending:
        if not eligible(s) or pending["fingerprint"] != fingerprint(s):
            decision(state, 'cancelled', 'changed', pending=None, armed=g['status'] == 'active')
            return False
        decision(state, 'backoff', 'due' if now >= pending['due'] else 'backoff')
        return now >= pending["due"]
    if (new_turn and state.get("armed") and eligible(s)
            and t["id"] not in state["attempted"]
            and t["completedAt"] >= int(state["first_seen"])
            and (t.get("startedAt") or 0) >= previous_started):
        correlated = g["status"] in ("active", "blocked")
        if g["status"] == "paused" and isinstance(t.get("startedAt"), (int, float)):
            # Allow only a pause during this failed turn, not a later user pause.
            correlated = (t["startedAt"] * 1000 <= g["updated_at_ms"]
                          <= t["completedAt"] * 1000 + 2000)
        if correlated:
            delay = min(60 * (2 ** min(state["attempts"], 3)), 300) * (1 + jitter)
            decision(state, 'backoff', 'backoff',
                     pending={'fingerprint': fingerprint(s), 'due': now+delay})
            return False
    if t.get('id') in state['attempted']:
        reason = 'unknown_outcome' if state.get('recovery', {}).get('outcome') == 'unknown' else 'already_attempted'
        decision(state, 'awaiting_new_turn', reason)
    elif key == state.get('baseline_turn') and t.get('status') == 'failed':
        decision(state, 'watching', 'historical_failure', armed=g['status'] == 'active')
    elif g["status"] == "paused":
        decision(state, 'paused', 'manual_pause', armed=False)
    elif t.get("status") == "completed":
        decision(state, 'healthy', 'healthy', armed=True, attempts=0)
    else:
        decision(state, 'watching', 'watching', armed=g['status'] == 'active')
    return False


def evaluate(state, s, now, jitter=0.0, config=None, target=None):
    config, target = config or {}, target or {}
    enabled = target.get('enabled', True)
    old_guard = state.get('_guard_revision')
    had_state = bool(state)
    old_enabled = state.get('_enabled', True)
    old_policy = state.get('_policy_revision')
    stats = state.get('stats', {})
    last_failure = state.get('last_failure')
    recorded_failure = state.get('recorded_failure')
    changed_binding = had_state and (old_guard != target.get('guard_revision')
                                     or old_policy != config.get('policy_revision'))
    if changed_binding or (had_state and old_enabled != enabled):
        state.update(last_turn=turn_key(s), baseline_turn=turn_key(s), first_seen=now,
                     pending=None, armed=(s.get('goal') or {}).get('status') == 'active')
    due = _evaluate(state, s, now, jitter, config)
    state.update(_enabled=enabled, _guard_revision=target.get('guard_revision'),
                 _policy_revision=config.get('policy_revision'), stats=stats)
    if last_failure and 'last_failure' not in state:
        state['last_failure'] = last_failure
    if recorded_failure:
        state['recorded_failure'] = recorded_failure
    if not enabled:
        state.update(last_turn=turn_key(s), baseline_turn=turn_key(s), first_seen=now)
        decision(state, 'guard_paused', 'guard_paused', pending=None, armed=False)
        due = False
    elif changed_binding or (had_state and not old_enabled):
        decision(state, 'baseline', 'guard_resumed', pending=None)
        due = False
    state['_baseline_applied'] = changed_binding or (had_state and old_enabled != enabled)
    turn, goal = s.get('turn') or {}, s.get('goal') or {}
    activity = [turn_key(s), goal.get('tokens_used'), s.get('updated_at')]
    if state.get('activity_key') != activity:
        state.update(activity_key=activity, last_activity_at=now)
    threshold = config.get('stall_after_seconds', 0)
    state['stalled'] = bool(threshold and (s.get('runtime') == 'active' or turn.get('status') == 'inProgress')
                            and now-state.get('last_activity_at', now) >= threshold)
    recovery = state.get('recovery')
    if recovery and recovery.get('goal_id') == goal.get('goal_id'):
        if (turn.get('id') != recovery['failed_turn']
                and turn.get('status') in ('inProgress', 'completed')
                and (turn.get('startedAt') or 0) >= recovery['attempted_at']-2):
            if recovery.get('stage') == 'awaiting':
                recovery.update(stage='running', running_at=now,
                                downtime_seconds=max(0, now-recovery['failure_at']))
            if turn.get('status') == 'completed':
                recovery.update(stage='completed', completed_at=now)
    state['recovery_unconfirmed'] = bool(recovery and recovery.get('goal_id') == goal.get('goal_id')
        and goal.get('status') not in TERMINAL and recovery.get('stage') == 'awaiting'
        and now-recovery['attempted_at'] >= config.get('recovery_confirmation_seconds', 300))
    failure = state.get('last_failure') or {}
    state['attention'] = bool(state['stalled'] or state['recovery_unconfirmed'] or
        (enabled and goal.get('status') not in TERMINAL and turn.get('status') == 'failed' and failure.get('at') is not None
         and now-failure['at'] >= config.get('attention_after_seconds', 1800)))
    return due


def record_observation(events, previous, state, s, now):
    """Record meaningful transitions, never one event per successful poll."""
    tid, turn, goal = s['thread_id'], s.get('turn') or {}, s.get('goal') or {}
    stats = state.setdefault('stats', {})
    def emit(kind, **data):
        return events.record(kind, tid, now=now, goal_id=goal.get('goal_id'),
                             turn_id=turn.get('id'), **data)
    if turn.get('status') == 'failed':
        pattern = classify(turn.get('error'))
        error = turn.get('error') or {}
        failure_key = [turn.get('id'), turn.get('completedAt')]
        if state.get('recorded_failure') != failure_key:
            message = safe_text(error.get('message', '')) if isinstance(error, dict) else safe_text(error)
            raw_code = error.get('codexErrorInfo') if isinstance(error, dict) else None
            code = safe_text(next(iter(raw_code), '')) if isinstance(raw_code, dict) else safe_text(raw_code) if raw_code else None
            failure = {'turn_id': turn.get('id'), 'at': turn.get('completedAt') or now,
                       'message': message, 'code': code, 'pattern': pattern.id if pattern else None,
                       'pattern_name': pattern.name if pattern else '未匹配规则'}
            state.update(recorded_failure=failure_key, last_failure=failure)
            stats['failures'] = stats.get('failures', 0)+1
            emit('failure_detected', message=message, pattern=failure['pattern'],
                 reason=state.get('reason'), historical=state.get('phase') == 'baseline')
    if state.get('pending') and not previous.get('pending'):
        emit('recovery_scheduled', due=state['pending']['due'], reason=state['reason'])
    elif previous.get('pending') and not state.get('pending'):
        emit('recovery_cancelled', reason=state['reason'])
    if state.get('_enabled') != previous.get('_enabled', True) or state.get('_baseline_applied'):
        emit('guard_resumed' if state['_enabled'] else 'guard_paused', reason=state['reason'])
    recovery, old = state.get('recovery') or {}, previous.get('recovery') or {}
    if recovery.get('stage') in ('running', 'completed') and old.get('stage') == 'awaiting':
        emit('recovery_running', downtime_seconds=recovery['downtime_seconds'])
        stats['downtime_seconds'] = stats.get('downtime_seconds', 0)+recovery['downtime_seconds']
    if recovery.get('stage') == 'completed' and old.get('stage') != 'completed':
        emit('recovery_completed')
        stats['recoveries'] = stats.get('recoveries', 0)+1
    if goal.get('status') == 'complete' and state.get('recorded_complete') != goal.get('goal_id'):
        emit('goal_completed')
        state['recorded_complete'] = goal.get('goal_id')
    if state.get('attention') and not previous.get('attention'):
        emit('attention', reason=('长时间没有可观测的新活动' if state['stalled'] else
                                 '恢复后未观察到新轮次' if state['recovery_unconfirmed'] else '失败持续未恢复'))


def attempt(state, observed, read_again, persist, resume, stopped, now):
    """Revalidate, durably consume one failure, then perform the sole mutation."""
    if stopped():
        return False
    fresh = read_again()
    if stopped() or not eligible(fresh) or fingerprint(fresh) != fingerprint(observed):
        decision(state, 'cancelled_before_resume', 'changed_before_resume', pending=None)
        persist()
        return False
    if not state.get("pending") or state["pending"]["fingerprint"] != fingerprint(fresh):
        return False
    tid = fresh["turn"]["id"]
    if tid in state["attempted"]:
        return False
    state["attempted"] = (state["attempted"] + [tid])[-256:]
    decision(state, 'resume_intent_saved', 'resume_intent', pending=None,
             attempts=state['attempts']+1, last_attempt_at=now,
             recovery={'goal_id': fresh['goal']['goal_id'], 'failed_turn': tid,
                       'failure_at': fresh['turn']['completedAt'], 'attempted_at': now,
                       'stage': 'awaiting', 'outcome': 'pending'})
    persist()  # A lost response or a restart cannot repeat this turn's mutation.
    if stopped():
        return False
    try:
        result = resume()
        if not isinstance(result.get("goal"), dict) or result["goal"].get("status") != "active":
            raise ValueError("resume did not return an active goal")
    except Exception:
        decision(state, 'resume_outcome_unknown', 'unknown_outcome')
        state['recovery']['outcome'] = 'unknown'
        persist()
        raise
    decision(state, 'resume_accepted', 'resume_accepted')
    state['recovery']['outcome'] = 'accepted'
    persist()
    return True


def summary(s):
    g, t = s.get("goal") or {}, s.get("turn") or {}
    return {"title": s.get("title"), "runtime": s.get("runtime"), "goal_status": g.get("status"),
            "goal_id": g.get("goal_id"), "turn_id": t.get("id"),
            "turn_status": t.get("status"), "capacity_error": capacity(t.get("error"))}


def configure(args, config):
    """CLI and TUI share the same serialized allowlist operations."""
    if args.remove:
        remove_session(args.config, args.remove)
        print("Removed; takes effect at the next observation (normally within 5 seconds).")
    elif args.add:
        target = add_session(args.config, args.add, args.label)
        print("Added; existing failed turns will be baselined, not resumed.")
        print(json.dumps(target, ensure_ascii=False))
    else:
        result = discover_sessions(args.config)
        for target in result["sessions"]:
            print(json.dumps(target, ensure_ascii=False))
        for warning in result["warnings"]:
            print("Warning: "+warning)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", help="print read-only snapshots and exit")
    parser.add_argument("--observe", action="store_true", help="never mutate goals")
    parser.add_argument('--parent-pid', type=int, help=argparse.SUPPRESS)
    edits = parser.add_mutually_exclusive_group()
    edits.add_argument("--add", metavar="THREAD_ID", help="register a main thread")
    edits.add_argument("--remove", metavar="THREAD_ID", help="stop guarding a thread")
    edits.add_argument("--discover", action="store_true", help="list loaded main threads read-only")
    parser.add_argument("--label", help="optional display name for --add")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.add or args.remove or args.discover:
        configure(args, config)
        return
    state_dir = Path(config["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    handler = RotatingFileHandler(state_dir / "watchdog.log", maxBytes=1024*1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    lock = (state_dir / ("observe.lock" if args.observe or args.once else "watchdog.lock")).open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    prefix = "observe-" if args.observe else ""
    state_path = state_dir / (prefix + "state.json")
    state = load_state(state_path)
    events = EventLog(state_dir, observe=args.observe)
    notifier = Notifier(state_dir) if not args.observe and not args.once else None
    # An interrupted recovery intent is consumed, never replayed at startup.
    stop = [False]
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.__setitem__(0, True))
    stopped = lambda: (stop[0] or (not args.once and (state_dir / 'STOP').exists())
                       or (args.parent_pid is not None and os.getppid() != args.parent_pid))
    client = None
    status = {"pid": os.getpid(), "host": os.uname().nodename,
              "observe_only": args.observe or args.once, "targets": {}}
    LOG.info("started pid=%s observe=%s targets=%s", os.getpid(), args.observe or args.once,
             [x["thread_id"] for x in config["targets"]])
    last_error = None
    try:
        while not stopped():
            try:
                latest_config = load_config(args.config)
                if any(latest_config[k] != config[k] for k in ("socket_path", "codex_home", "state_dir")):
                    raise ValueError("connection/storage paths changed; restart the watchdog")
                config = latest_config
                allowed = {t["thread_id"] for t in config["targets"]}
                status["targets"] = {k: v for k, v in status["targets"].items() if k in allowed}
                removed = set(state) - allowed
                if removed:
                    for tid in removed:
                        del state[tid]
                        status["targets"].pop(tid, None)
                    if not args.once:
                        save_state(state_path, state)
                    LOG.info("removed targets=%s", sorted(removed))
                for target in config["targets"]:
                    if stopped():
                        break
                    tid = target["thread_id"]
                    try:
                        if client is None:
                            client = Client(config["socket_path"])
                        current = snapshot(client, target, config["codex_home"])
                        if args.once:
                            print(json.dumps({"label": target["label"], "thread_id": tid,
                                              **summary(current)}, ensure_ascii=False))
                            continue
                        item = state.setdefault(tid, {})
                        if item.get("_registration") != target.get("registration_id"):
                            item.clear()
                        previous_phase = item.get("phase")
                        old = copy.deepcopy(item)
                        now = time.time()
                        policy_config = {**config, 'observe_only': args.observe or config.get('observe_only', False)}
                        due = evaluate(item, current, now, random.uniform(0, 0.1), policy_config, target)
                        record_observation(events, old, item, current, now)
                        item["_registration"] = target.get("registration_id")
                        if item != old:
                            save_state(state_path, state)
                        if previous_phase != item["phase"]:
                            LOG.info("thread=%s phase=%s", tid, item["phase"])
                        def no_longer_allowed():
                            current_config = load_config(args.config)
                            return (stopped() or any(current_config[k] != config[k]
                                    for k in ("socket_path", "codex_home", "state_dir")) or not any(
                                x["thread_id"] == tid and x["cwd"] == target["cwd"]
                                and x.get("registration_id") == target.get("registration_id")
                                and x.get('enabled', True)
                                and x.get('guard_revision') == target.get('guard_revision')
                                for x in current_config["targets"]) or
                                current_config.get('policy_revision') != config.get('policy_revision') or
                                current_config.get('observe_only', False) or
                                pattern_mode(current_config, classify(current['turn'].get('error'))) != 'recover')
                        def persist_attempt():
                            save_state(state_path, state)
                            kind = {'resume_intent_saved': 'resume_attempted',
                                    'resume_accepted': 'resume_accepted',
                                    'resume_outcome_unknown': 'resume_unknown',
                                    'cancelled_before_resume': 'recovery_cancelled'}.get(item['phase'])
                            if kind:
                                events.record(kind, tid, reason=item['reason'],
                                              turn_id=current['turn']['id'])
                                if kind in ('resume_attempted', 'resume_accepted'):
                                    counter = 'attempts' if kind == 'resume_attempted' else 'accepted'
                                    item['stats'][counter] = item['stats'].get(counter, 0)+1
                                save_state(state_path, state)
                        if due and not args.observe and not no_longer_allowed():
                            LOG.info("revalidation thread=%s turn=%s", tid, current["turn"]["id"])
                            try:
                                accepted = attempt(
                                    item, current,
                                    lambda: snapshot(client, target, config['codex_home']),
                                    persist_attempt,
                                    lambda: client.request('thread/goal/set', {
                                        'threadId': tid, 'status': 'active', 'origin': 'user'
                                    }), no_longer_allowed, time.time())
                            except Exception as exc:
                                if item.get('phase') != 'resume_outcome_unknown':
                                    raise
                                accepted = False
                                LOG.warning('thread=%s resume outcome unknown: %s', tid, safe_text(str(exc)))
                                client.close()
                                client = None
                            LOG.info("resume_attempt thread=%s accepted=%s", tid, accepted)
                        previous_row = status['targets'].get(tid, {})
                        if previous_row.get('error'):
                            events.record('observation_restored', tid)
                        status["targets"][tid] = {"label": target["label"], **summary(current),
                                                   "registration_id": target.get("registration_id"),
                                                   "phase": item["phase"], "attempts": item["attempts"],
                                                   'checked_at': now, 'last_success_at': now,
                                                   'outage_started_at': None,
                                                   'reason': item.get('reason'), 'reason_code': item.get('reason_code'),
                                                   'last_failure': item.get('last_failure'), 'stats': item.get('stats', {}),
                                                   'stalled': item.get('stalled'), 'attention': item.get('attention'),
                                                   'recovery_unconfirmed': item.get('recovery_unconfirmed'),
                                                   'recovery': item.get('recovery'),
                                                   'guard_revision': target.get('guard_revision'),
                                                   "next_retry_at": (item.get("pending") or {}).get("due")}
                        status['last_success_at'] = now
                        if notifier:
                            try:
                                enabled_pattern = pattern_mode(config, classify((current.get('turn') or {}).get('error'))) != 'off'
                                if item.get('attention') and enabled_pattern:
                                    notifier.notify(config, tid, 'attention', target['label']+'：需要检查持续失败或停顿')
                                if (enabled_pattern and item.get('reason_code') in ('unmatched_failure', 'observe_only', 'manual_pause', 'blocked', 'unknown_outcome')
                                        and not config.get('observe_only') and target.get('enabled', True)):
                                    notifier.notify(config, tid, 'manual_attention', target['label']+'：'+item['reason'])
                                if (current.get('goal') or {}).get('status') == 'complete' and (old.get('recorded_complete') != item.get('recorded_complete')):
                                    notifier.notify(config, tid, 'goal_completed', target['label']+'：Goal 已完成')
                            except OSError as exc:
                                LOG.warning('notification storage failed: %s', safe_text(str(exc)))
                    except Exception as exc:
                        if args.once:
                            raise
                        message = safe_text(f"{type(exc).__name__}: {exc}")
                        if status["targets"].get(tid, {}).get("error") != message:
                            LOG.warning("thread=%s suspended: %s", tid, message)
                            events.record('observation_failed', tid, message=message)
                        old_row = status['targets'].get(tid, {})
                        status["targets"][tid] = {"label": target["label"], "phase": "suspended", "error": message,
                            'checked_at': time.time(), 'last_success_at': old_row.get('last_success_at'),
                            'reason': '读取失败，无法确认会话状态；停止恢复操作',
                            'reason_code': 'observation_error', 'registration_id': target.get('registration_id'),
                            'guard_revision': target.get('guard_revision'),
                            'last_failure': old_row.get('last_failure'), 'stats': old_row.get('stats', {})}
                        outage_start = old_row.get('last_success_at') or old_row.get('outage_started_at') or time.time()
                        status['targets'][tid]['outage_started_at'] = outage_start
                        if notifier and time.time()-outage_start >= config.get('attention_after_seconds', 1800):
                            notifier.notify(config, tid, 'monitor_unavailable', target['label']+'：长时间无法读取 Codex 状态')
                        if client:
                            client.close()
                            client = None
                last_error = None
                status.pop("error", None)
            except Exception as exc:
                message = safe_text(f"{type(exc).__name__}: {exc}")
                if message != last_error:
                    LOG.warning("observation/recovery suspended: %s", message)
                    last_error = message
                status["error"] = message
                if client:
                    client.close()
                    client = None
                if args.once:
                    raise
            if args.once:
                break
            status["checked_at"] = time.time()
            atomic_json(state_dir / (prefix + "status.json"), status)
            wait_for_next_check(args.config,config,status,state,
                                state_dir / (prefix + "status.json"),stopped)
    finally:
        if client:
            client.close()
        LOG.info("stopped pid=%s", os.getpid())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import sys
        print(safe_text(f'{type(exc).__name__}: {exc}'), file=sys.stderr)
        sys.exit(1)
