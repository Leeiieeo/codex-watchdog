'use strict';

const { spawnSync } = require('node:child_process');
const { accessSync, constants, realpathSync } = require('node:fs');
const { delimiter, join, isAbsolute } = require('node:path');

const PROBE = [
  'import sys',
  'if sys.version_info < (3, 10): raise SystemExit(2)',
  'import curses, sqlite3, fcntl',
].join('\n');

function executablePaths(command, env) {
  if (isAbsolute(command) || command.includes('/')) return [command];
  return (env.PATH || '').split(delimiter).filter(Boolean).map(directory => join(directory, command));
}

function findPython(env = process.env) {
  const explicit = env.CODEX_WATCHDOG_PYTHON;
  const commands = explicit ? [explicit] : [
    'python3', ...Array.from({ length: 7 }, (_, i) => `python3.${16 - i}`), 'python',
  ];
  const visited = new Set();
  for (const command of commands) {
    for (const candidate of executablePaths(command, env)) {
      let path;
      try {
        accessSync(candidate, constants.X_OK);
        path = realpathSync(candidate);
      } catch {
        continue;
      }
      if (visited.has(path)) continue;
      visited.add(path);
      const result = spawnSync(path, ['-I', '-c', PROBE], {
        env, encoding: 'utf8', timeout: 3000, maxBuffer: 65536,
      });
      if (!result.error && result.status === 0) return path;
    }
  }
  throw new Error(explicit
    ? 'CODEX_WATCHDOG_PYTHON must point to Python 3.10+ with curses, sqlite3 and fcntl.'
    : 'Python 3.10+ with curses and sqlite3 is required. Install it using your system package manager, or set CODEX_WATCHDOG_PYTHON to an existing interpreter. uv and pip are not required.');
}

module.exports = { findPython };
