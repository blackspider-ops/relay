"""Wait for local filesystem changes; never fetch allowance or run inference."""

import asyncio
import json
import os
from pathlib import Path
import select


def fingerprint(value):
    """Heartbeats and sample time do not represent a dashboard change."""
    value = {**value, "at": None}
    value["sessions"] = [
        {k: v for k, v in item.items() if k not in {"seen", "activity"}}
        for item in value.get("sessions", [])
    ]
    return json.dumps(value, sort_keys=True)


class LocalChanges:
    def __init__(self, state):
        self.state = Path(state)
        self.queue = select.kqueue() if hasattr(select, "kqueue") else None
        self.files = []
        self.signature = None

    def close(self):
        if self.queue:
            self.queue.close()
        for fd in self.files:
            os.close(fd)
        self.files.clear()

    def wait(self, timeout=30):
        paths = [
            self.state,
            self.state / "services",
            self.state / "accounts",
            self.state / "pool.sqlite",
            self.state / "pool.sqlite-wal",
            self.state / "settings.json",
        ]
        if not self.queue:
            import time

            time.sleep(timeout)
            return
        for fd in self.files:
            os.close(fd)
        self.files = []
        registrations = []
        for path in paths:
            try:
                fd = os.open(path, os.O_RDONLY)
            except FileNotFoundError:
                continue
            self.files.append(fd)
            registrations.append(
                select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_RENAME
                    | select.KQ_NOTE_DELETE
                    | select.KQ_NOTE_ATTRIB,
                )
            )
        self.queue.control(registrations, max(1, len(registrations)), timeout)

    async def changed(self, timeout=30):
        await asyncio.to_thread(self.wait, timeout)
        # Coalesce a burst of updates into one consistent snapshot.
        await asyncio.sleep(0.15)
