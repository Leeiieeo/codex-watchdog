import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_watchdog import __version__


class CliTests(unittest.TestCase):
    def test_module_entry_runs_from_an_unrelated_directory(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'PYTHONPATH': str(root/'src')}
            for argument in ('--version', '--help'):
                with self.subTest(argument=argument):
                    result = subprocess.run([sys.executable, '-m', 'codex_watchdog', argument],
                                            cwd=directory, env=env, capture_output=True,
                                            text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('codex-watchdog', result.stdout)
                    if argument == '--version':
                        self.assertEqual(result.stdout.strip(), 'codex-watchdog '+__version__)
