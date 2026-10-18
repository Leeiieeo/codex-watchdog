#!/bin/sh
set -eu
task_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$task_dir/src${PYTHONPATH:+:$PYTHONPATH}"
exec "${CODEX_WATCHDOG_PYTHON:-python3}" -m codex_watchdog.control "$@"
