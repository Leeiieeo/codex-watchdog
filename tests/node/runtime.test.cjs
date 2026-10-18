'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { mkdtempSync, writeFileSync, rmSync, mkdirSync, realpathSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join, delimiter } = require('node:path');
const { spawnSync } = require('node:child_process');
const { findPython } = require('../../bin/runtime.cjs');

test('runtime search rejects old Python and continues along PATH', () => {
  const root = mkdtempSync(join(tmpdir(), 'watchdog-runtime-'));
  try {
    for (const [directory, code] of [['old', 2], ['working', 0]]) {
      mkdirSync(join(root, directory));
      writeFileSync(join(root, directory, 'python3'), `#!/bin/sh\nexit ${code}\n`, { mode: 0o755 });
    }
    const path = [join(root, 'old'), join(root, 'working')].join(delimiter);
    assert.equal(findPython({ PATH: path }), realpathSync(join(root, 'working', 'python3')));
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('an explicit incompatible runtime is rejected without falling back', () => {
  const root = mkdtempSync(join(tmpdir(), 'watchdog-runtime-'));
  try {
    const bad = join(root, 'bad-python');
    writeFileSync(bad, '#!/bin/sh\nexit 2\n', { mode: 0o755 });
    assert.throws(() => findPython({ ...process.env, CODEX_WATCHDOG_PYTHON: bad }),
      /CODEX_WATCHDOG_PYTHON must point to Python 3\.10\+/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('missing runtime gives actionable help without exposing probe output', () => {
  assert.throws(() => findPython({ PATH: '/nonexistent' }), /uv and pip are not required/);
  const root = mkdtempSync(join(tmpdir(), 'watchdog-runtime-'));
  try {
    const bad = join(root, 'python');
    writeFileSync(bad, '#!/bin/sh\nprintf "private-probe-output" >&2\nexit 1\n', { mode: 0o755 });
    const result = spawnSync(process.execPath, [join(__dirname, '../../bin/codex-watchdog.cjs'), '--version'], {
      env: { ...process.env, CODEX_WATCHDOG_PYTHON: bad }, encoding: 'utf8', timeout: 5000,
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /Python 3\.10\+/);
    assert.doesNotMatch(result.stderr, /private-probe-output/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
