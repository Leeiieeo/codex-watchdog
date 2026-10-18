"""Wait between Codex observations while accepting local configuration changes."""
import logging
import time

from .registry import load_config
from .storage import atomic_json

LOG = logging.getLogger('codex-watchdog')


def wait_for_next_check(config_path, config, status, state, status_path, stopped):
    started = time.monotonic()
    valid = config
    last_error = None
    while not stopped():
        try:
            valid = load_config(config_path)
            last_error = None
        except (OSError,ValueError,KeyError,TypeError) as exc:
            message = str(exc)
            if message != last_error:
                LOG.warning('configuration refresh failed: %s',message)
            last_error = message
        interval = valid['check_interval_seconds']
        next_check = status['checked_at']+interval
        changed = (status.get('check_interval_seconds') != interval
                   or status.get('next_check_at') != next_check)
        if status.get('check_interval_seconds') != interval:
            LOG.info('check_interval_seconds=%s',interval)
        status.update(check_interval_seconds=interval,next_check_at=next_check)
        for tid,row in status['targets'].items():
            due = (state.get(tid,{}).get('pending') or {}).get('due')
            retry = max(due,next_check) if due is not None else None
            changed = changed or row.get('next_retry_at') != retry
            row['next_retry_at'] = retry
        if changed:
            atomic_json(status_path,status)
        # Explicit enrollment changes should establish a baseline promptly.
        same_binding = all(valid[k] == config[k] for k in ('socket_path','codex_home','state_dir'))
        if same_binding and valid['targets'] != config['targets']:
            return
        remaining = started+interval-time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1,remaining))
