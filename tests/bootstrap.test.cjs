const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const version = require('../package.json').version;
const { stateDirectory, fingerprint, ownerAlive, withSetupLock } = require('../bin/relay.cjs');

test('fresh installs use Relay; existing account homes stay in place', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-home-'));
  try {
    assert.equal(stateDirectory({}, home), path.join(home, 'Library/Application Support/Relay'));
    fs.mkdirSync(path.join(home, 'Library/Application Support/Codex Accounts/accounts'), { recursive: true });
    assert.equal(stateDirectory({}, home), path.join(home, 'Library/Application Support/Codex Accounts'));
    assert.equal(stateDirectory({ RELAY_STATE: '/custom/state' }, home), '/custom/state');
  } finally { fs.rmSync(home, { recursive: true, force: true }); }
});

test('runtime identity includes the shipped code and locked dependencies', () => {
  const bundle = fingerprint();
  assert.ok(bundle.id.startsWith(version + '-'));
  assert.match(bundle.id.slice(version.length + 1), /^[a-f0-9]{16}$/);
  assert.ok(bundle.names.includes('requirements.lock'));
  assert.ok(bundle.names.includes('router.py'));
  assert.ok(!bundle.names.some(name => name.startsWith('test_')));
});

test('first-run setup serializes concurrent terminal launches', async () => {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-setup-'));
  let active = 0, maximum = 0;
  try {
    await Promise.all(Array.from({ length: 6 }, () => withSetupLock(state, async () => {
      maximum = Math.max(maximum, ++active);
      await new Promise(resolve => setTimeout(resolve, 10));
      active--;
    })));
    assert.equal(maximum, 1);
  } finally { fs.rmSync(state, { recursive: true, force: true }); }
});

test('interrupted setup locks are recoverable and process identity is checked', async () => {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-stale-'));
  const lock = path.join(state, 'relay-setup.lock');
  try {
    fs.mkdirSync(lock);
    fs.writeFileSync(path.join(lock, 'owner.json'), JSON.stringify({ pid: process.pid, birth: 'another process generation' }));
    assert.equal(ownerAlive({ pid: process.pid, birth: 'wrong generation' }), false);
    let ran = false;
    await withSetupLock(state, async () => { ran = true; });
    assert.ok(ran); assert.ok(!fs.existsSync(lock));
  } finally { fs.rmSync(state, { recursive: true, force: true }); }
});

test('version works before Python setup and needs no account login', () => {
  const result = spawnSync(process.execPath, [path.join(__dirname, '../bin/relay.cjs'), '--version'], { encoding: 'utf8' });
  if (process.platform === 'darwin') {
    assert.equal(result.status, 0); assert.equal(result.stdout.trim(), `Relay ${version}`);
  } else { assert.notEqual(result.status, 0); }
});
