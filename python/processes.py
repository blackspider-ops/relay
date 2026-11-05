"""Reap detached children while their launcher remains alive."""

import subprocess
import threading


def spawn(*args, **kwargs):
    process = subprocess.Popen(*args, **kwargs)
    threading.Thread(
        target=process.wait, daemon=True, name="codex-child-reaper"
    ).start()
    return process
