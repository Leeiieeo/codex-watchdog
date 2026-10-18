#!/usr/bin/env node
'use strict';

const { spawn } = require('node:child_process');
const { join, delimiter } = require('node:path');
const { homedir } = require('node:os');
const { findPython } = require('./runtime.cjs');

if (!['linux', 'darwin'].includes(process.platform)) {
  process.stderr.write('codex-watchdog requires Linux or macOS; on Windows use WSL.\n');
  process.exitCode = 1;
} else {
  let python;
  try {
    python = findPython();
  } catch (error) {
    process.stderr.write(`codex-watchdog: ${error.message}\n`);
    process.exitCode = 1;
  }
  if (python) {
    const source = join(__dirname, '..', 'src');
    const env = {
      ...process.env,
      PYTHONPATH: source + (process.env.PYTHONPATH ? delimiter + process.env.PYTHONPATH : ''),
      CODEX_WATCHDOG_LAUNCHER_PID: String(process.pid),
      CODEX_WATCHDOG_CONFIG: process.env.CODEX_WATCHDOG_CONFIG
        || join(process.env.XDG_CONFIG_HOME || join(homedir(), '.config'), 'codex-watchdog', 'config.json'),
    };
    // A separate process group makes terminal Ctrl+C arrive exactly once:
    // the Node launcher forwards it to the Python UI, which owns its worker.
    const child = spawn(python, ['-m', 'codex_watchdog', ...process.argv.slice(2)], {
      env, stdio: 'inherit', detached: true,
    });
    const signals = ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGWINCH'];
    const forward = new Map();
    for (const signal of signals) {
      const handler = () => {
        if (child.pid && child.exitCode === null && child.signalCode === null) {
          try { child.kill(signal); } catch { /* The child may have just exited. */ }
        }
      };
      forward.set(signal, handler);
      process.on(signal, handler);
    }
    child.on('error', () => {
      process.stderr.write('codex-watchdog: unable to start the Python runtime.\n');
      process.exitCode = 1;
    });
    child.on('close', (code, signal) => {
      for (const [name, handler] of forward) process.off(name, handler);
      process.exitCode = code ?? (signal ? 1 : 0);
    });
  }
}
