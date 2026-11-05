#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync, spawn } = require('node:child_process');
const { setTimeout: delay } = require('node:timers/promises');

const packageRoot = path.resolve(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8'));

function stateDirectory(env = process.env, home = os.homedir()) {
  if (env.RELAY_STATE || env.CODEX_ACCOUNTS_STATE) {
    return path.resolve(env.RELAY_STATE || env.CODEX_ACCOUNTS_STATE);
  }
  const support = path.join(home, 'Library', 'Application Support');
  const legacy = path.join(support, 'Codex Accounts');
  return fs.existsSync(path.join(legacy, 'accounts')) ? legacy : path.join(support, 'Relay');
}

function pythonCommand() {
  const choices = [process.env.RELAY_PYTHON, 'python3', 'python3.14', 'python3.13', 'python3.12', 'python3.11'].filter(Boolean);
  for (const command of choices) {
    const result = spawnSync(command, ['-c', 'import sys; print(sys.executable); raise SystemExit(sys.version_info < (3, 11))'], { encoding: 'utf8' });
    if (result.status === 0) return result.stdout.trim();
  }
  throw new Error('Relay needs Python 3.11 or newer. Install it with: brew install python@3.13');
}

function privateDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(directory);
  if (info.isSymbolicLink() || (process.getuid && info.uid !== process.getuid())) {
    throw new Error('Relay needs a private folder owned by your user.');
  }
  fs.chmodSync(directory, 0o700);
}

function fingerprint() {
  const source = path.join(packageRoot, 'python');
  const names = fs.readdirSync(source).filter(name => /\.(py|swift|lock|txt)$/.test(name)).sort();
  for (const name of fs.readdirSync(path.join(packageRoot, 'tests')).filter(name => name.endsWith('.py')).sort()) names.push(`tests/${name}`);
  const hash = crypto.createHash('sha256');
  hash.update(pkg.version);
  for (const name of names) hash.update(name).update(fs.readFileSync(name.startsWith('tests/') ? path.join(packageRoot, name) : path.join(source, name)));
  return { id: `${pkg.version}-${hash.digest('hex').slice(0, 16)}`, names, source };
}

function processBirth(pid) {
  const result = spawnSync('/bin/ps', ['-p', String(pid), '-o', 'lstart='], { encoding: 'utf8' });
  return result.status === 0 ? result.stdout.trim() : '';
}

function ownerAlive(owner) {
  try {
    process.kill(owner.pid, 0);
    return Boolean(owner.birth) && processBirth(owner.pid) === owner.birth;
  } catch { return false; }
}

async function withSetupLock(state, operation) {
  const lock = path.join(state, 'relay-setup.lock');
  const deadline = Date.now() + 10 * 60 * 1000;
  while (true) {
    try { fs.mkdirSync(lock, { mode: 0o700 }); break; }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      let stale = false;
      try {
        const owner = JSON.parse(fs.readFileSync(path.join(lock, 'owner.json'), 'utf8'));
        stale = !ownerAlive(owner);
      } catch {
        // Another process may be between mkdir and writing its owner record.
        try { stale = Date.now() - fs.statSync(lock).mtimeMs > 30_000; }
        catch (error) { if (error.code === 'ENOENT') continue; throw error; }
      }
      if (stale) {
        const abandoned = `${lock}.stale-${process.pid}-${crypto.randomBytes(4).toString('hex')}`;
        try { fs.renameSync(lock, abandoned); fs.rmSync(abandoned, { recursive: true, force: true }); }
        catch (error) { if (!['ENOENT', 'EEXIST'].includes(error.code)) throw error; }
        continue;
      }
      if (Date.now() > deadline) throw new Error('Another Relay setup is still running. Let it finish, then try again.');
      await delay(200);
    }
  }
  fs.writeFileSync(path.join(lock, 'owner.json'), JSON.stringify({ pid: process.pid, birth: processBirth(process.pid) }), { mode: 0o600 });
  try { return await operation(); }
  finally { fs.rmSync(lock, { recursive: true, force: true }); }
}

function run(command, args) {
  const result = spawnSync(command, args, { stdio: 'inherit' });
  if (result.error || result.status !== 0) throw new Error('Setup did not finish. Run Relay again to retry.');
}

async function prepare(state) {
  const bundle = fingerprint();
  const destination = path.join(state, 'relay-runtimes', bundle.id);
  const interpreter = path.join(destination, '.venv', 'bin', 'python');
  const marker = path.join(destination, 'ready.json');
  const ready = () => fs.existsSync(marker) && fs.existsSync(interpreter) && spawnSync(interpreter, ['-c', 'import websockets, jsonschema'], { stdio: 'ignore' }).status === 0;
  if (ready()) return { interpreter, router: path.join(destination, 'router.py') };
  const python = pythonCommand();
  return withSetupLock(state, async () => {
    if (ready()) return { interpreter, router: path.join(destination, 'router.py') };
    privateDirectory(destination);
    for (const name of bundle.names) {
      const source = name.startsWith('tests/') ? path.join(packageRoot, name) : path.join(bundle.source, name);
      fs.mkdirSync(path.dirname(path.join(destination, name)), { recursive: true, mode: 0o700 });
      fs.copyFileSync(source, path.join(destination, name));
    }
    console.error('Preparing Relay for this Mac. This only happens on first use or after an update.');
    // Always retry dependency installation if a previous setup was interrupted.
    run(python, ['-m', 'venv', path.join(destination, '.venv')]);
    run(interpreter, ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', path.join(destination, 'requirements.lock')]);
    fs.writeFileSync(marker, JSON.stringify({ version: pkg.version }), { mode: 0o600 });
    return { interpreter, router: path.join(destination, 'router.py') };
  });
}

async function main() {
  if (process.platform !== 'darwin') throw new Error('Relay currently supports macOS.');
  if (process.argv.length === 3 && ['--version', '-V'].includes(process.argv[2])) {
    console.log(`Relay ${pkg.version}`); return;
  }
  const state = stateDirectory(); privateDirectory(state);
  const runtime = await prepare(state);
  const child = spawn(runtime.interpreter, [runtime.router, ...process.argv.slice(2)], {
    stdio: 'inherit', env: { ...process.env, RELAY_STATE: state, CODEX_ACCOUNTS_STATE: state }
  });
  // Terminal Ctrl-C already reaches both foreground processes.
  const interrupt = () => {};
  const terminate = () => child.kill('SIGTERM');
  process.on('SIGINT', interrupt); process.on('SIGTERM', terminate);
  process.on('SIGHUP', terminate);
  child.on('error', () => { console.error('Relay could not start. Run it again to retry.'); process.exitCode = 1; });
  await new Promise(resolve => child.on('close', (code, signal) => {
    process.exitCode = code ?? (signal === 'SIGINT' ? 130 : 1); resolve();
  }));
  process.off('SIGINT', interrupt); process.off('SIGTERM', terminate);
  process.off('SIGHUP', terminate);
}

module.exports = { stateDirectory, fingerprint, ownerAlive, withSetupLock, prepare };
if (require.main === module) main().catch(error => { console.error(`relay: ${error.message}`); process.exitCode = 1; });
