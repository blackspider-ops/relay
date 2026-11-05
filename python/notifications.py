"""Opt-in local macOS notifications with a bounded, metadata-only message."""

import subprocess
import time
from processes import spawn

_last = {}
EVENTS = {
    "switched": "Account switched",
    "waiting_for_reset": "Waiting for account reset",
    "needs_attention": "Codex needs attention",
    "completed": "Codex task finished",
    "backend_restored": "Codex session recovered",
    "model_fallback": "Configured model fallback selected",
}


def notify(kind, label=""):
    if kind not in EVENTS or time.monotonic() - _last.get(kind, 0) < 30:
        return
    _last[kind] = time.monotonic()
    # argv is passed to AppleScript as data, never interpolated as script code.
    script = (
        'on run argv\ndisplay notification (item 1 of argv) with title "Relay"\nend run'
    )
    spawn(
        [
            "/usr/bin/osascript",
            "-e",
            script,
            EVENTS[kind] + (" — " + label if label else ""),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
