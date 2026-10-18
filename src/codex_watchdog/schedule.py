"""Normal polling plus independent recovery and reconnection deadlines."""
import logging
import time

from .registry import load_config
from .storage import atomic_json

LOG = logging.getLogger('codex-watchdog')


def planned_check(config, status, state):
    checked = status['checked_at']
    deadline = checked+config['check_interval_seconds']
    if status.get('error'):
        for row in status['targets'].values():
            row['next_retry_at'] = None
        return min(deadline, checked+30)
    for tid, row in status['targets'].items():
        item = state.get(tid, {})
        if row.get('error'):
            deadline = min(deadline, checked+30)
            # Reconnection needs to succeed before a past-due recovery can be
            # revalidated; do not spin on an expired pending deadline.
            row['next_retry_at'] = None
            continue
        pending = item.get('pending')
        row['next_retry_at'] = pending['due'] if pending else None
        if pending:
            deadline = min(deadline, max(checked, pending['due']))
        recovery = item.get('recovery') or {}
        if recovery.get('stage') == 'awaiting' and not item.get('recovery_unconfirmed') and row.get('goal_status') not in ('complete','budget_limited','usage_limited'):
            deadline = min(deadline, checked+min(10, config.get('recovery_confirmation_seconds', 300)))
    return deadline


def wait_for_next_check(config_path, config, status, state, status_path, stopped):
    started = time.monotonic()
    valid = config
    last_error = None
    while not stopped():
        try:
            valid = load_config(config_path)
            last_error = None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            message = str(exc)
            if message != last_error:
                LOG.warning('configuration refresh failed: %s', message)
            last_error = message
        next_check = planned_check(valid, status, state)
        changed = (status.get('check_interval_seconds') != valid['check_interval_seconds']
                   or status.get('next_check_at') != next_check)
        status.update(check_interval_seconds=valid['check_interval_seconds'], next_check_at=next_check)
        if changed:
            atomic_json(status_path, status)
        same_binding = all(valid[k] == config[k] for k in ('socket_path', 'codex_home', 'state_dir'))
        if same_binding and any(valid.get(k) != config.get(k) for k in
                ('targets', 'pattern_modes', 'observe_only', 'policy_revision', 'stall_after_seconds')):
            return
        remaining = started+(next_check-status['checked_at'])-time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1, remaining))
