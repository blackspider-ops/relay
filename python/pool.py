"""Cross-process coordination. Contains usage metadata, never tokens or prompts."""

from __future__ import annotations
import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import subprocess
import functools


@contextlib.asynccontextmanager
async def file_lock(path, *, shared=False):
    """Nonblocking flock with cancellable waiting, never block the event loop."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(
                    fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
                )
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        os.close(fd)


def buckets(snapshot, model=None):
    values = snapshot.get("rateLimitsByLimitId")
    if not values:
        return [snapshot.get("rateLimits") or {}]
    return [
        v
        for k, v in values.items()
        if k == "codex" or (model and v.get("normalModelSlug") == model)
    ]


def blocked(snapshot, model=None):
    return snapshot.get("ordinaryUsageAllowed") is False or any(
        b.get("rateLimitReachedType") or b.get("spendControlReached")
        for b in buckets(snapshot, model)
    )


def reset_time(snapshot, model=None, now=None):
    """For simultaneous exhausted windows, all must reset: take the latest.

    A timestamp only schedules a fresh read; it never establishes recovery.
    """
    now = time.time() if now is None else now
    windows = [
        b.get(k) or {}
        for b in buckets(snapshot, model)
        for k in ("primary", "secondary")
    ]
    exhausted = [w for w in windows if w.get("usedPercent", 0) >= 100]
    dates = [w.get("resetsAt") for w in exhausted]
    if dates and all(isinstance(d, (int, float)) and d > now for d in dates):
        return max(dates) + 2  # Small grace for server clock/propagation.
    return None


class Pool:
    def __init__(self, state: Path, *, clock=time.time):
        self.state, self.clock = state, clock
        self.session = uuid.uuid4().hex
        self.path = state / "pool.sqlite"
        with self.db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS usage(label TEXT PRIMARY KEY, checked REAL, snapshot TEXT);
              CREATE TABLE IF NOT EXISTS blocks(label TEXT, scope TEXT, until REAL, reason TEXT,
                PRIMARY KEY(label,scope));
              CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,pid INTEGER,label TEXT);
              CREATE TABLE IF NOT EXISTS threads(thread TEXT PRIMARY KEY,owner TEXT,pid INTEGER);
              CREATE TABLE IF NOT EXISTS account_options(label TEXT PRIMARY KEY, options TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS session_details(id TEXT PRIMARY KEY, birth TEXT, seen REAL, details TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS reservations(owner TEXT PRIMARY KEY,label TEXT,until REAL);
              CREATE TABLE IF NOT EXISTS intents(owner TEXT,thread TEXT,details TEXT,PRIMARY KEY(owner,thread));
              CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL,owner TEXT,kind TEXT,details TEXT);
              CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL,label TEXT,snapshot TEXT);
              CREATE TABLE IF NOT EXISTS commands(id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT,op TEXT,details TEXT,at REAL,status TEXT);
              CREATE TABLE IF NOT EXISTS signals(name TEXT PRIMARY KEY,revision INTEGER);
            """)
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def prune(self, db):
        for table, key in [("sessions", "id"), ("threads", "thread")]:
            for ident, pid in db.execute(f"SELECT {key},pid FROM {table}").fetchall():
                row = db.execute(
                    "SELECT birth FROM session_details WHERE id=?",
                    (
                        ident
                        if table == "sessions"
                        else (
                            db.execute(
                                "SELECT owner FROM threads WHERE thread=?", (ident,)
                            ).fetchone()
                            or [""]
                        )[0],
                    ),
                ).fetchone()
                birth_matches = not row or not row[0] or row[0] == process_birth(pid)
                if not self.alive(pid) or not birth_matches:
                    db.execute(f"DELETE FROM {table} WHERE {key}=?", (ident,))
        db.execute(
            "DELETE FROM reservations WHERE until<? OR owner NOT IN (SELECT id FROM sessions)",
            (self.clock(),),
        )

    def assign(self, label):
        with self.db() as db:
            self.prune(db)
            db.execute(
                "INSERT OR REPLACE INTO sessions VALUES(?,?,?)",
                (self.session, os.getpid(), label),
            )
            db.execute("DELETE FROM reservations WHERE owner=?", (self.session,))
        self.touch(account=label)

    def loads(self):
        with self.db() as db:
            self.prune(db)
            rows = dict(
                db.execute(
                    "SELECT label,count(*) FROM sessions WHERE id<>? GROUP BY label",
                    (self.session,),
                )
            )
            for label, count in db.execute(
                "SELECT label,count(*) FROM reservations WHERE owner<>? GROUP BY label",
                (self.session,),
            ):
                rows[label] = rows.get(label, 0) + count
            # Public loads still include this session for status and compatibility.
            own = db.execute(
                "SELECT label FROM sessions WHERE id=?", (self.session,)
            ).fetchone()
            if own:
                rows[own[0]] = rows.get(own[0], 0) + 1
            return rows

    def reserve(self, label, expected_load=None, limit=0, ttl=120):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.prune(db)
            row = db.execute(
                "SELECT options FROM account_options WHERE label=?", (label,)
            ).fetchone()
            options = json.loads(row[0]) if row else {}
            if maintenance_active(options) or not options.get("enabled", True):
                return False
            configured = options.get("max_sessions", 0)
            limit = (
                min(limit, configured) if limit and configured else limit or configured
            )
            current = db.execute(
                "SELECT count(*) FROM sessions WHERE label=? AND id<>?",
                (label, self.session),
            ).fetchone()[0]
            current += db.execute(
                "SELECT count(*) FROM reservations WHERE label=? AND owner<>?",
                (label, self.session),
            ).fetchone()[0]
            own = db.execute(
                "SELECT label FROM sessions WHERE id=?", (self.session,)
            ).fetchone()
            compare = current + (1 if own and own[0] == label else 0)
            if (
                limit
                and current >= limit
                or expected_load is not None
                and compare > expected_load
            ):
                return False
            # Reserve before any network call. No inference is moved until login succeeds.
            db.execute(
                "INSERT OR IGNORE INTO sessions VALUES(?,?,?)",
                (self.session, os.getpid(), ""),
            )
            db.execute(
                "INSERT OR REPLACE INTO reservations VALUES(?,?,?)",
                (self.session, label, self.clock() + ttl),
            )
        self.touch()
        return True

    def begin_maintenance(self, label):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.prune(db)
            if (
                db.execute("SELECT 1 FROM sessions WHERE label=?", (label,)).fetchone()
                or db.execute(
                    "SELECT 1 FROM reservations WHERE label=?", (label,)
                ).fetchone()
            ):
                return False
            row = db.execute(
                "SELECT options FROM account_options WHERE label=?", (label,)
            ).fetchone()
            options = json.loads(row[0]) if row else {}
            if maintenance_active(options):
                return False
            options.update(
                maintenance=True,
                maintenance_pid=os.getpid(),
                maintenance_birth=process_birth(os.getpid()),
            )
            db.execute(
                "INSERT OR REPLACE INTO account_options VALUES(?,?)",
                (label, json.dumps(options)),
            )
        self.signal("accounts")
        self.signal()
        return True

    def release_reservation(self):
        with self.db() as db:
            db.execute("DELETE FROM reservations WHERE owner=?", (self.session,))

    def renew_reservation(self, ttl=120):
        with self.db() as db:
            db.execute(
                "UPDATE reservations SET until=? WHERE owner=? AND until>?",
                (self.clock() + ttl, self.session, self.clock()),
            )

    def touch(self, **details):
        with self.db() as db:
            row = db.execute(
                "SELECT details FROM session_details WHERE id=?", (self.session,)
            ).fetchone()
            value = json.loads(row[0]) if row else {}
            value.update(details)
            db.execute(
                "INSERT OR REPLACE INTO session_details VALUES(?,?,?,?)",
                (
                    self.session,
                    process_birth(os.getpid()),
                    self.clock(),
                    json.dumps(value),
                ),
            )

    def detail(self):
        with self.db() as db:
            self.prune(db)
            rows = db.execute(
                "SELECT s.id,s.pid,s.label,d.seen,d.details FROM sessions s LEFT JOIN session_details d ON s.id=d.id"
            ).fetchall()
        return [
            {
                **json.loads(r[4] or "{}"),
                "id": r[0],
                "pid": r[1],
                "account": r[2],
                "seen": r[3],
            }
            for r in rows
        ]

    def options(self, label):
        with self.db() as db:
            row = db.execute(
                "SELECT options FROM account_options WHERE label=?", (label,)
            ).fetchone()
        value = {
            "enabled": True,
            "priority": 0,
            "max_sessions": 0,
            "models": [],
            **(json.loads(row[0]) if row else {}),
        }
        if value.get("maintenance") and not maintenance_active(value):
            value["maintenance"] = False
        return value

    def set_options(self, label, **values):
        value = self.options(label)
        value.update(values)
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO account_options VALUES(?,?)",
                (label, json.dumps(value)),
            )
        self.signal("accounts")
        self.signal()

    def signal(self, name="wake"):
        with self.db() as db:
            db.execute(
                "INSERT INTO signals VALUES(?,1) ON CONFLICT(name) DO UPDATE SET revision=revision+1",
                (name,),
            )

    def revision(self, name="wake"):
        with self.db() as db:
            row = db.execute(
                "SELECT revision FROM signals WHERE name=?", (name,)
            ).fetchone()
        return row[0] if row else 0

    def intent(self, thread, details=None):
        with self.db() as db:
            if details is None:
                row = db.execute(
                    "SELECT details FROM intents WHERE owner=? AND thread=?",
                    (self.session, thread),
                ).fetchone()
                return json.loads(row[0]) if row else None
            db.execute(
                "INSERT OR REPLACE INTO intents VALUES(?,?,?)",
                (self.session, thread, json.dumps(details)),
            )

    def record(self, kind, **details):
        # Callers use fixed reason codes and metadata, never raw exceptions/payloads.
        with self.db() as db:
            import settings

            limit = settings.load(self.state)["event_limit"]
            db.execute(
                "INSERT INTO events(at,owner,kind,details) VALUES(?,?,?,?)",
                (self.clock(), self.session, kind, json.dumps(details)),
            )
            db.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (limit,),
            )

    def command(self, owner, op, details=None):
        with self.db() as db:
            cursor = db.execute(
                "INSERT INTO commands(owner,op,details,at,status) VALUES(?,?,?,?,?)",
                (owner, op, json.dumps(details or {}), self.clock(), "pending"),
            )
        self.signal()
        return cursor.lastrowid

    def take_commands(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,op,details FROM commands WHERE owner=? AND status='pending' ORDER BY id",
                (self.session,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE commands SET status='received' WHERE id=?", (row[0],)
                )
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def ack(self, command, status):
        with self.db() as db:
            db.execute("UPDATE commands SET status=? WHERE id=?", (status, command))

    def claim(self, thread):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.prune(db)
            row = db.execute(
                "SELECT owner FROM threads WHERE thread=?", (thread,)
            ).fetchone()
            if row and row[0] != self.session:
                return False
            db.execute(
                "INSERT OR REPLACE INTO threads VALUES(?,?,?)",
                (thread, self.session, os.getpid()),
            )
            return True

    def owner(self, thread):
        with self.db() as db:
            self.prune(db)
            row = db.execute(
                "SELECT owner FROM threads WHERE thread=?", (thread,)
            ).fetchone()
            return row[0] if row else None

    def release_thread(self, thread):
        with self.db() as db:
            db.execute(
                "DELETE FROM threads WHERE thread=? AND owner=?", (thread, self.session)
            )

    def close(self):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (self.session,))
            db.execute("DELETE FROM threads WHERE owner=?", (self.session,))
            db.execute("DELETE FROM reservations WHERE owner=?", (self.session,))

    def save(self, label, snapshot):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO usage VALUES(?,?,?)",
                (label, self.clock(), json.dumps(snapshot)),
            )
            db.execute(
                "INSERT INTO history(at,label,snapshot) VALUES(?,?,?)",
                (self.clock(), label, json.dumps(snapshot)),
            )
            import settings

            db.execute(
                "DELETE FROM history WHERE at<?",
                (self.clock() - settings.load(self.state)["history_days"] * 86400,),
            )

    def cached(self, label, max_age=60, not_before=None):
        with self.db() as db:
            row = db.execute(
                "SELECT checked,snapshot FROM usage WHERE label=?", (label,)
            ).fetchone()
        if (
            row
            and self.clock() - row[0] < max_age
            and (not_before is None or row[0] >= not_before)
        ):
            return json.loads(row[1])
        return None

    def mark(self, label, model=None, *, snapshot=None, reason="quota", delay=300):
        if snapshot is not None:
            self.save(label, snapshot)
        until = (
            reset_time(snapshot or {}, model, self.clock())
            if reason == "quota"
            else None
        )
        until = until or self.clock() + delay
        scope = model or "*"
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)",
                (label, scope, until, reason),
            )
        return until

    def deadline(self, label, model=None):
        with self.db() as db:
            rows = db.execute(
                "SELECT until FROM blocks WHERE label=? AND (scope='*' OR scope=?)",
                (label, model or "*"),
            ).fetchall()
        return max((row[0] for row in rows), default=0)

    def invalidate(self, label):
        with self.db() as db:
            db.execute("DELETE FROM usage WHERE label=?", (label,))

    def clear(self, label, model=None):
        with self.db() as db:
            db.execute(
                "DELETE FROM blocks WHERE label=? AND (scope='*' OR scope=?)",
                (label, model or "*"),
            )

    def reset_identity(self, label):
        with self.db() as db:
            for table in ("usage", "blocks", "history"):
                db.execute(f"DELETE FROM {table} WHERE label=?", (label,))
        self.signal()


def process_birth(pid):
    if pid == os.getpid():
        return _own_birth()
    return _birth(pid)


def maintenance_active(options):
    if not options.get("maintenance"):
        return False
    pid = options.get("maintenance_pid")
    return (
        pid is None
        or Pool.alive(pid)
        and process_birth(pid) == options.get("maintenance_birth")
    )


def _birth(pid):
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@functools.lru_cache(maxsize=1)
def _own_birth():
    return _birth(os.getpid())


CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."


def failure_kind(info, message=None):
    if info == "policyCapacityUnavailable":
        return "capacity"
    # Older/generic error envelopes can omit the enum. Match only the known
    # terminal capacity message, never broad words like "policy" or "capacity".
    if (
        info in (None, "other", "serverOverloaded", "flexUnavailable")
        and isinstance(message, str)
        and message.strip().removeprefix("■").strip() == CAPACITY_MESSAGE
    ):
        return "capacity"
    if isinstance(info, str):
        if info == "usageLimitExceeded":
            return "quota"
        if info == "unauthorized":
            return "auth"
        if info in {
            "rateLimitExceeded",
            "internalServerError",
            "serverOverloaded",
            "flexUnavailable",
        }:
            return "transient"
        return None
    if isinstance(info, dict):
        for key in [
            "httpConnectionFailed",
            "responseStreamConnectionFailed",
            "responseStreamDisconnected",
            "responseTooManyFailedAttempts",
        ]:
            if key in info:
                status = (info[key] or {}).get("httpStatusCode")
                if status == 401:
                    return "auth"
                if (
                    status is None
                    or isinstance(status, int)
                    and (
                        status in (408, 429)
                        or 500 <= status < 600
                        or status == 200
                        and key
                        in {
                            "responseStreamDisconnected",
                            "responseTooManyFailedAttempts",
                        }
                    )
                ):
                    return "transient"
    return None
