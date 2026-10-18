"""Observe the optional npm launcher without changing direct Python startup."""
import os


def launcher_gone():
    expected = os.environ.get('CODEX_WATCHDOG_LAUNCHER_PID')
    return expected is not None and str(os.getppid()) != expected
