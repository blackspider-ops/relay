const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json')));
const router = fs.readFileSync(path.join(root, 'python/router.py'), 'utf8');
assert.ok(router.includes(`VERSION = "${pkg.version}"`), 'Python and npm versions differ');
const pack = spawnSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts'], { cwd: root, encoding: 'utf8' });
assert.equal(pack.status, 0, pack.stderr);
const files = JSON.parse(pack.stdout)[0].files.map(file => file.path);
for (const name of files) {
  assert.ok(!/(__pycache__|\.pyc$|\.venv|auth\.json|\.sqlite|connection\.json|job\.json|verification|v0_)/i.test(name), `Private or obsolete file in package: ${name}`);
}
assert.ok(files.includes('bin/relay.cjs'));
assert.ok(files.includes('python/router.py'));
assert.ok(files.includes('LICENSE'));
console.log(`Package checked: ${files.length} files; version ${pkg.version}.`);
