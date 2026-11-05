# Contributing

Use macOS, Node.js 20+, and Python 3.11+. No account login is needed for the tests.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r python/requirements.lock
PYTHONPATH=python:tests .venv/bin/python -m unittest discover -s tests -q
npm test
npm run check
```

Keep changes small. A recovery change should include a test showing when the next
turn may run and how duplicate actions are avoided. Never use real credentials in
fixtures or commit local state. See [publishing](docs/publishing.md) for releases.
