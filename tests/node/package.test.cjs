'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { mkdtempSync, readFileSync, rmSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join, resolve } = require('node:path');
const { spawnSync } = require('node:child_process');
const { findPython } = require('../../bin/runtime.cjs');

const root = resolve(__dirname, '../..');
const metadata = require('../../package.json');

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: 'utf8', timeout: 30000, ...options });
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  return result.stdout;
}

test('npm archive includes runtime source and excludes private files', () => {
  const target = mkdtempSync(join(tmpdir(), 'watchdog-pack-'));
  try {
    const output = JSON.parse(run('npm', ['pack', '--json', '--ignore-scripts', '--pack-destination', target], { cwd: root }));
    const files = output[0].files.map(file => file.path);
    assert.ok(files.includes('bin/codex-watchdog.cjs'));
    assert.ok(files.includes('bin/runtime.cjs'));
    assert.ok(files.includes('src/codex_watchdog/watchdog.py'));
    assert.ok(files.includes('src/codex_watchdog/__main__.py'));
    assert.ok(!files.some(file => /(^|\/)(state|tests|\.env|node_modules|__pycache__)(\/|$)/.test(file)));
    assert.ok(!files.includes('config.json'));
    assert.ok(!files.includes('pyproject.toml'));
    assert.equal(metadata.version, readFileSync(join(root, 'src/codex_watchdog/__init__.py'), 'utf8').match(/"([^"]+)"/)[1]);
  } finally { rmSync(target, { recursive: true, force: true }); }
});

test('global npm installation runs from an unrelated directory without uv or pip', () => {
  const target = mkdtempSync(join(tmpdir(), 'watchdog-install-'));
  try {
    const packed = JSON.parse(run('npm', ['pack', '--json', '--ignore-scripts', '--pack-destination', target], { cwd: root }))[0];
    run('npm', ['install', '--global', '--prefix', join(target, 'global'), '--ignore-scripts',
      '--no-audit', '--no-fund', join(target, packed.filename)], { cwd: target });
    const binary = join(target, 'global/bin/codex-watchdog');
    const env = { ...process.env, CODEX_WATCHDOG_PYTHON: findPython() };
    delete env.PYTHONPATH;
    delete env.CODEX_WATCHDOG_CONFIG;
    env.XDG_CONFIG_HOME = join(target, 'user-config');
    assert.equal(run(binary, ['--version'], { cwd: target, env }).trim(), `codex-watchdog ${metadata.version}`);
    assert.match(run(binary, ['--help'], { cwd: target, env }), /usage: codex-watchdog/);
    const path = join(target, 'config.json');
    writeFileSync(path, JSON.stringify({ observe_only: true, codex_home: target,
      socket_path: join(target, 'socket'), state_dir: join(target, 'state'), targets: [] }));
    assert.match(run(binary, ['--config', path, 'status'], { cwd: target, env }), /Watchdog: stopped/);
    const probe = ['import os', 'from codex_watchdog.setup import default_config', 'print(default_config())'].join('\n');
    env.PYTHONPATH = join(target, 'global/lib/node_modules/codex-watchdog/src');
    assert.equal(run(env.CODEX_WATCHDOG_PYTHON, ['-c', probe], { cwd: target, env }).trim(),
      join(target, 'user-config/codex-watchdog/config.json'));
  } finally { rmSync(target, { recursive: true, force: true }); }
});
