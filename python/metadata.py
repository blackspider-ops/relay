"""Recover local routing metadata without touching credentials or conversations."""

import contextlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from pool import Pool, process_birth


def restore_backup(state):
    backup = state / "pool.backup.sqlite"
    if not backup.is_file():
        raise ValueError("No metadata backup exists; use doctor --backup first")
    # Supervised workers are identified independently of the potentially damaged DB.
    for path in (state / "services").glob("*.supervisor.json"):
        try:
            value = json.loads(path.read_text())
        except (ValueError, OSError):
            raise ValueError(
                "Unreadable supervisor metadata; inspect it before restoring"
            ) from None
        if Pool.alive(value["pid"]) and process_birth(value["pid"]) == value.get(
            "birth"
        ):
            raise ValueError(
                "Stop wrapper sessions before restoring metadata; account logins are retained"
            )
    database = state / "pool.sqlite"
    if database.exists():
        with contextlib.suppress(sqlite3.Error):
            db = sqlite3.connect(database)
            try:
                if any(
                    Pool.alive(row[0]) for row in db.execute("SELECT pid FROM sessions")
                ):
                    raise ValueError("Stop wrapper sessions before restoring metadata")
            finally:
                db.close()
    temporary = state / ("restore-" + uuid.uuid4().hex + ".sqlite")
    source = sqlite3.connect("file:" + str(backup) + "?mode=ro", uri=True)
    try:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Metadata backup is corrupt")
        dest = sqlite3.connect(temporary)
        try:
            source.backup(dest)
            tables = {
                r[0]
                for r in dest.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"usage", "blocks", "sessions", "threads"} <= tables:
                raise ValueError("Not a Relay metadata backup")
            # Old process/command records must never become live after restore.
            for table in (
                "sessions",
                "threads",
                "reservations",
                "session_details",
                "commands",
            ):
                if table in tables:
                    dest.execute("DELETE FROM " + table)
            dest.commit()
        finally:
            dest.close()
        temporary.chmod(0o600)
        quarantine = state / ("metadata-before-restore-" + str(time.time_ns()))
        quarantine.mkdir(mode=0o700)
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(database) + suffix)
            if path.exists():
                path.replace(quarantine / path.name)
        temporary.replace(database)
        return str(quarantine)
    finally:
        source.close()
        temporary.unlink(missing_ok=True)
