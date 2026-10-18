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
from .storage import atomic_json
from .registry import load_config, add_session, remove_session, discover_sessions
from .schedule import wait_for_next_check

LOG = logging.getLogger("codex-watchdog")
TERMINAL = {"complete", "usage_limited", "budget_limited"}
STATUS_MAP = {"usageLimited": "usage_limited", "budgetLimited": "budget_limited"}
CAPACITY_MESSAGE = "selected model is at capacity. please try a different model."


def capacity(error):
    if not isinstance(error, dict):
        return False
    code = error.get("codexErrorInfo")
    return code == "serverOverloaded" or (
        code in (None, "other")
        and str(error.get("message", "")).strip().lower() == CAPACITY_MESSAGE
    )


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
            t.get("id"), t.get("completedAt"), t.get("error")]


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


def evaluate(state, s, now, jitter=0.0):
    """Mutate only watchdog state. Return True only for a due recovery candidate."""
    g, t = s.get("goal") or {}, s.get("turn") or {}
    key = turn_key(s)
    if not state or state.get("goal_id") != g.get("goal_id"):
        state.clear()
        state.update(goal_id=g.get("goal_id"), last_turn=key,
                     armed=g.get("status") == "active", pending=None,
                     attempts=0, attempted=[], phase="baseline", first_seen=now,
                     newest_started_at=t.get("startedAt") or 0)
        return False
    new_turn = state["last_turn"] != key
    previous_started = state.get("newest_started_at", 0)
    state["newest_started_at"] = max(previous_started, t.get("startedAt") or 0)
    state["last_turn"] = key
    if s.get("runtime") not in ("idle", "active", "systemError"):
        state.update(pending=None, armed=False, phase="unavailable")
        return False
    if (not g or g.get("status") in TERMINAL
            or (g.get("status") not in ("active", "paused") and not eligible(s))):
        state.update(pending=None, armed=False, attempts=0, phase="goal_stopped")
        return False
    if s["runtime"] == "active" or t.get("status") == "inProgress":
        state.update(pending=None, armed=g["status"] == "active", phase="running")
        return False
    pending = state.get("pending")
    if pending:
        if not eligible(s) or pending["fingerprint"] != fingerprint(s):
            state.update(pending=None, armed=g["status"] == "active", phase="cancelled")
            return False
        state["phase"] = "backoff"
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
            state.update(pending={"fingerprint": fingerprint(s), "due": now + delay},
                         phase="backoff")
            return False
    if g["status"] == "paused":
        state.update(armed=False, phase="paused")
    elif t.get("status") == "completed":
        state.update(armed=True, attempts=0, phase="healthy")
    elif t.get("id") in state["attempted"]:
        state["phase"] = "awaiting_new_turn"
    else:
        state.update(armed=g["status"] == "active", phase="watching")
    return False


def attempt(state, observed, read_again, persist, resume, stopped, now):
    """Revalidate, durably consume one failure, then perform the sole mutation."""
    if stopped():
        return False
    fresh = read_again()
    if stopped() or not eligible(fresh) or fingerprint(fresh) != fingerprint(observed):
        state.update(pending=None, phase="cancelled_before_resume")
        persist()
        return False
    if not state.get("pending") or state["pending"]["fingerprint"] != fingerprint(fresh):
        return False
    tid = fresh["turn"]["id"]
    if tid in state["attempted"]:
        return False
    state["attempted"] = (state["attempted"] + [tid])[-256:]
    state.update(pending=None, attempts=state["attempts"] + 1,
                 last_attempt_at=now, phase="resume_intent_saved")
    persist()  # A lost response or a restart cannot repeat this turn's mutation.
    if stopped():
        return False
    try:
        result = resume()
        if not isinstance(result.get("goal"), dict) or result["goal"].get("status") != "active":
            raise ValueError("resume did not return an active goal")
    except Exception:
        state["phase"] = "resume_outcome_unknown"
        persist()
        raise
    state["phase"] = "resume_accepted"
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
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    # An interrupted recovery intent is consumed, never replayed at startup.
    stop = [False]
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.__setitem__(0, True))
    stopped = lambda: stop[0] or (not args.once and (state_dir / "STOP").exists())
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
                        atomic_json(state_path, state)
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
                        due = evaluate(item, current, time.time(), random.uniform(0, 0.1))
                        item["_registration"] = target.get("registration_id")
                        if item != old:
                            atomic_json(state_path, state)
                        if previous_phase != item["phase"]:
                            LOG.info("thread=%s phase=%s", tid, item["phase"])
                        def no_longer_allowed():
                            current_config = load_config(args.config)
                            return (stopped() or any(current_config[k] != config[k]
                                    for k in ("socket_path", "codex_home", "state_dir")) or not any(
                                x["thread_id"] == tid and x["cwd"] == target["cwd"]
                                and x.get("registration_id") == target.get("registration_id")
                                for x in current_config["targets"]))
                        if due and not args.observe and not no_longer_allowed():
                            LOG.info("revalidation thread=%s turn=%s", tid, current["turn"]["id"])
                            accepted = attempt(
                                item, current,
                                lambda: snapshot(client, target, config["codex_home"]),
                                lambda: atomic_json(state_path, state),
                                lambda: client.request("thread/goal/set", {
                                    "threadId": tid, "status": "active", "origin": "user"
                                }), no_longer_allowed, time.time()
                            )
                            LOG.info("resume_attempt thread=%s accepted=%s", tid, accepted)
                        status["targets"][tid] = {"label": target["label"], **summary(current),
                                                   "registration_id": target.get("registration_id"),
                                                   "phase": item["phase"], "attempts": item["attempts"],
                                                   "next_retry_at": (item.get("pending") or {}).get("due")}
                    except Exception as exc:
                        if args.once:
                            raise
                        message = f"{type(exc).__name__}: {exc}"
                        if status["targets"].get(tid, {}).get("error") != message:
                            LOG.warning("thread=%s suspended: %s", tid, message)
                        status["targets"][tid] = {"label": target["label"], "phase": "suspended", "error": message}
                        if client:
                            client.close()
                            client = None
                last_error = None
                status.pop("error", None)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
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
    main()
