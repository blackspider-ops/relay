#!/usr/bin/env python3
"""Private, experimental Codex CLI account controller. Never log RPC payloads."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
import uuid

from pool import Pool, blocked, reset_time, failure_kind, file_lock
from launch import translate, RESUME_FIELDS
import settings

VERSION = "0.5.1"
_support = Path.home() / "Library/Application Support"
_legacy = _support / "Codex Accounts"
_default_state = _legacy if (_legacy / "accounts").exists() else _support / "Relay"
STATE = Path(
    os.environ.get("RELAY_STATE")
    or os.environ.get("CODEX_ACCOUNTS_STATE")
    or _default_state
)
CONTINUE = (
    "Continue the original task from its saved state after a service interruption. "
    "Review the existing conversation and completed tool results before acting. "
    "Do not repeat completed commands or external actions. Preserve all existing "
    "user requirements and permissions. Verify any uncertain partial operation first."
)


class RouterError(Exception):
    """A message safe to display; never include a server error or credential."""


class RecoveryTransition(Exception):
    """Return to the recovery driver without growing the Python call stack."""

    def __init__(self, kind):
        self.kind = kind


_recovery_active = contextvars.ContextVar("relay_recovery_active", default=None)


class RemoteError(RouterError):
    def __init__(self, payload):
        super().__init__(
            "Codex rejected an RPC operation; no account change was assumed."
        )
        self.payload = payload


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RouterError("State directories must not be symlinks.")
    path.chmod(0o700)
    return path


def clean_env(home: Path) -> dict:
    env = dict(os.environ)
    # The pool must not accidentally select an environment-provided identity.
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(key, None)
    env["CODEX_HOME"] = str(home)
    return env


def codex_path() -> str:
    path = shutil.which("codex")
    if not path:
        raise RouterError("Codex CLI is not installed or not on PATH.")
    return path


def account_home(state: Path, label: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}", label):
        raise RouterError(
            "Use a short account label containing letters, numbers, - or _."
        )
    return state / "accounts" / label


def labels(state: Path) -> list[str]:
    root = state / "accounts"
    return (
        sorted(
            p.name for p in root.iterdir() if p.is_dir() and (p / "enrolled").exists()
        )
        if root.exists()
        else []
    )


def read_keychain(home: Path, *, include_identity=False) -> dict:
    """Native Codex direct-keyring layout in 0.166.0. stdout remains in memory."""
    key = "cli|" + hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "Codex Auth",
                "-a",
                key,
                "-w",
            ],
            capture_output=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        raise RouterError(
            "Keychain access timed out. Check for a macOS Keychain prompt."
        ) from None
    if result.returncode:
        raise RouterError(
            "Cannot read this account's Keychain login. Unlock Keychain or enroll the account again."
        )
    try:
        auth = json.loads(result.stdout)
        tokens = auth["tokens"]
        token = tokens["access_token"]
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        metadata = claims.get("https://api.openai.com/auth", {})
        account_id = tokens.get("account_id") or metadata["chatgpt_account_id"]
        if not token or not account_id or auth.get("OPENAI_API_KEY"):
            raise ValueError()
        if claims.get("exp", 0) <= time.time() + 10:
            raise RouterError(
                "Account token expired; enroll this account again if automatic refresh fails."
            )
        result = {
            "accessToken": token,
            "chatgptAccountId": account_id,
            "chatgptPlanType": metadata.get("chatgpt_plan_type"),
        }
        if include_identity:
            result["email"] = claims.get("email") or claims.get(
                "https://api.openai.com/profile", {}
            ).get("email")
        return result
    except (KeyError, ValueError, TypeError, IndexError):
        raise RouterError(
            "This Keychain entry is not a usable ChatGPT login."
        ) from None


def limited(snapshot: dict, model: str | None = None) -> bool:
    """Only explicit server flags establish a block; percentages are advisory."""
    return blocked(snapshot, model)


class RPC:
    """Multiplex JSONL RPC. Only safe, fixed error messages leave this object."""

    def __init__(self, proc, handler=None):
        self.proc, self.handler = proc, handler
        self.pending = {}
        self.sequence = 0
        self.writer_lock = asyncio.Lock()
        self.handlers = set()
        self.reader = asyncio.create_task(self.read_loop())
        self.startup_guard = None

    @classmethod
    async def start(
        cls, binary: str, home: Path, *, helper=False, handler=None, extra_args=None
    ):
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        guard = file_lock(home / ".accounts-native-startup.lock")
        await guard.__aenter__()
        mode = "keyring" if helper else "ephemeral"
        cmd = [
            binary,
            "app-server",
            "--stdio",
        ]
        cmd.extend(extra_args or [])
        cmd += ["-c", f'cli_auth_credentials_store="{mode}"']
        if helper:
            cmd += ["-c", "features.secret_auth_storage=false"]
        else:
            # Let failures reach our finite 5/10/15-minute schedule instead of
            # the native client's optional unbounded reconnect loop.
            cmd += ["-c", "features.unbounded_connection_retries=false"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=clean_env(home),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=32 * 1024 * 1024,
            )
            rpc = cls(proc, handler)
            rpc.startup_guard = guard
            return rpc
        except BaseException:
            await guard.__aexit__(None, None, None)
            raise

    async def send(self, obj):
        async with self.writer_lock:
            try:
                self.proc.stdin.write(
                    json.dumps(obj, separators=(",", ":")).encode() + b"\n"
                )
                await self.proc.stdin.drain()
            except (ConnectionError, OSError):
                raise RouterError(
                    "Codex backend disconnected; resume the saved thread after restarting the wrapper."
                ) from None

    async def call(self, method, params=None, timeout=45):
        if self.reader.done():
            raise RouterError(
                "Codex backend disconnected; resume the saved thread after restarting the wrapper."
            )
        self.sequence += 1
        request_id = f"accounts-{self.sequence}"
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        request = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        try:
            await self.send(request)
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def initialize(self):
        try:
            result = await self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "local_codex_accounts",
                        "title": "Relay",
                        "version": VERSION,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.send({"method": "initialized"})
        except BaseException:
            await self.close()
            raise
        await self.release_startup()
        return result

    async def release_startup(self):
        if self.startup_guard:
            guard, self.startup_guard = self.startup_guard, None
            await guard.__aexit__(None, None, None)

    async def read_loop(self):
        try:
            while raw := await self.proc.stdout.readline():
                message = json.loads(raw)
                if "method" not in message and message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RemoteError(message["error"]))
                        else:
                            future.set_result(message.get("result", {}))
                elif self.handler:
                    task = asyncio.create_task(self.handler(message))
                    self.handlers.add(task)
                    task.add_done_callback(self.handlers.discard)
        except (ValueError, OSError, asyncio.LimitOverrunError):
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RouterError("Codex backend disconnected."))

    async def close(self):
        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        await self.release_startup()
        self.reader.cancel()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(self.reader, *self.handlers, return_exceptions=True)


class Account:
    def __init__(self, label, home, binary, pool=None):
        self.label, self.home, self.binary = label, home, binary
        self.pool = pool
        self.rpc = None
        self.lock = asyncio.Lock()

    async def start(self):
        if self.rpc and self.rpc.proc.returncode is not None:
            await self.rpc.close()
            self.rpc = None
        if not self.rpc:
            self.rpc = await RPC.start(self.binary, self.home, helper=True)
            await self.rpc.initialize()

    @contextlib.asynccontextmanager
    async def operation(self):
        async with self.lock:
            async with file_lock(self.home / "credential.lock"):
                # Fresh process reads latest Keychain state. Never retain a stale
                # refresh token while another terminal rotates the same login.
                try:
                    await self.start()
                    yield self.rpc
                finally:
                    await self.close()
                    self.rpc = None

    async def credentials(self, refresh=False):
        async with self.operation() as rpc:
            response = await rpc.call("account/read", {"refreshToken": refresh})
            if not response.get("account"):
                raise RouterError("Account requires sign-in; enroll it again.")
            return await asyncio.to_thread(read_keychain, self.home)

    async def usage(self, force=False):
        async with self.lock:
            async with file_lock(self.home / "credential.lock"):
                cached = (
                    self.pool.cached(self.label) if self.pool and not force else None
                )
                if cached is None and force and self.pool:
                    # Concurrent failures may all request a fresh read. Reuse
                    # a just-completed, explicitly blocked snapshot for 1s.
                    recent = self.pool.cached(self.label, max_age=1)
                    if recent and limited(recent):
                        cached = recent
                if cached is not None:
                    return cached
                try:
                    await self.start()
                    snapshot = await self.rpc.call("account/rateLimits/read")
                    if self.pool:
                        self.pool.save(self.label, snapshot)
                    return snapshot
                finally:
                    await self.close()
                    self.rpc = None

    async def close(self):
        if self.rpc:
            await self.rpc.close()


class Controller:
    def __init__(
        self,
        accounts,
        backend,
        report=print,
        interval=300,
        *,
        pool=None,
        retry_interval=300,
        retry_attempts=3,
        clock=time.time,
        sleep=asyncio.sleep,
    ):
        self.accounts, self.backend, self.report = accounts, backend, report
        self.interval = interval
        self.index = 0
        self.client = None
        self.active_threads = set()
        self.user_generation = 0
        self.last_turn = {}
        self.seen_failures = set()
        self.recovery = None
        self.login_lock = asyncio.Lock()
        self.client_tasks = set()
        self.server_requests = set()
        self.pending_server_requests = {}
        self.replayed_requests = set()
        self.pool = pool
        self.retry_interval, self.retry_attempts = retry_interval, retry_attempts
        self.clock, self.sleep = clock, sleep
        self.episodes = {}
        self.local_deadlines = {}
        self.thread_settings = {}
        self.status_changed = asyncio.Event()
        self.launch_overrides = {}
        self.additional_roots = []
        self.preferences = (
            settings.load(pool.state) if pool else settings.DEFAULTS.copy()
        )
        self.paused = False
        self.background = False
        self.wake = asyncio.Event()
        self.force_retry = asyncio.Event()
        self.turn_ids = {}
        self.recovery_jobs = {}
        self.pending_intents = {}
        self.active_items = set()
        self.backend_factory = None
        self.restart_lock = asyncio.Lock()
        self.restart_task = None
        self.paused_uncertain = set()
        self.control_task = None
        self.stopping = asyncio.Event()
        self.extra_accounts = None
        self.starting = False
        self.on_thread = None
        self.on_detach = None
        self.project = os.getcwd()
        self.profile = None
        self.network_state = None
        self.awake_process = None
        self.fallback_tried = {}

    def account_name(self):
        if self.pool:
            from commands import account_identity

            return (
                account_identity(self.pool.state, self.account.label).get("email")
                or self.pool.options(self.account.label).get("name")
                or self.account.label
            )
        return self.account.label

    def attached(self):
        return bool(self.client) or self.background or self.starting

    def persist(self, thread, **values):
        if self.pool:
            previous = self.pool.intent(thread) or {}
            previous.update(values)
            previous["settings"] = self.thread_settings.get(thread, {})
            previous.setdefault(
                "role", "foreground" if thread in self.last_turn else "child"
            )
            self.pool.intent(thread, previous)

    def note(self, kind, **fields):
        if self.pool:
            self.pool.record(kind, **fields)
            if kind in {"working", "completed", "switched", "backend_restored"}:
                fields.setdefault("next_wake", None)
            self.pool.touch(state=kind, account=self.account.label, **fields)
        if self.preferences.get("notifications"):
            from notifications import notify

            notify(kind, self.account_name())

    def merge_settings(self, thread, params):
        allowed = {
            "model",
            "cwd",
            "approvalPolicy",
            "approvalsReviewer",
            "sandbox",
            "sandboxPolicy",
            "permissions",
            "effort",
            "serviceTier",
            "summary",
            "environments",
            "disabledPluginIds",
            "runtimeWorkspaceRoots",
            "modelProvider",
            "personality",
            "baseInstructions",
            "developerInstructions",
            "outputSchema",
            "cyberAccessProgram",
        }
        current = self.thread_settings.setdefault(thread, {})
        params = dict(params)
        if params.get("reasoningEffort") is not None:
            params["effort"] = params["reasoningEffort"]
        if params.get("sandboxPolicy"):
            policy = params["sandboxPolicy"].get("type")
            translated = {
                "dangerFullAccess": "danger-full-access",
                "readOnly": "read-only",
                "workspaceWrite": "workspace-write",
                "danger-full-access": "danger-full-access",
                "read-only": "read-only",
                "workspace-write": "workspace-write",
            }.get(policy)
            if translated:
                params["sandbox"] = translated
        for key in allowed:
            if params.get(key) is not None:
                current[key] = params[key]
        if "permissions" in params and params.get("permissions"):
            current.pop("sandbox", None)
            current.pop("sandboxPolicy", None)
        if params.get("sandbox") or params.get("sandboxPolicy"):
            current.pop("permissions", None)

    def hold_for_review(self, thread, reason="uncertain_action"):
        self.paused_uncertain.add(thread)
        self.persist(
            thread, review_required=True, review_reason=reason, state="needs_attention"
        )

    def clear_review(self, thread):
        self.paused_uncertain.discard(thread)
        self.persist(thread, review_required=False, review_reason=None)

    def can_continue(self, thread, generation):
        saved = self.pool.intent(thread) if self.pool else {}
        return (
            generation == self.user_generation
            and self.attached()
            and thread not in self.paused_uncertain
            and not (saved or {}).get("review_required", False)
        )

    async def latest(self, thread):
        result = await self.backend.call(
            "thread/turns/list",
            {
                "threadId": thread,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "summary",
            },
            timeout=15,
        )
        return (result.get("data") or [None])[0]

    async def reconcile(self, thread, baseline=None):
        """Read acceptance/status. Never replay a request merely because it timed out."""
        try:
            latest = await self.latest(thread)
            info = await self.backend.call(
                "thread/read", {"threadId": thread}, timeout=15
            )
        except (RouterError, asyncio.TimeoutError):
            return "unknown", None
        status = (info.get("thread") or {}).get("status", {}).get("type")
        if status == "active":
            self.active_threads.add(thread)
        elif status in {"idle", "systemError", "notLoaded"}:
            self.active_threads.discard(thread)
        if baseline == "__unknown_before_submission__" and not self.pending_intents.get(
            thread, {}
        ).get("accepted"):
            return "unknown", latest
        if latest and (baseline is None or latest.get("id") != baseline):
            if latest.get("status") == "inProgress":
                return "running", latest
            if latest.get("status") == "completed":
                return "completed", latest
            if latest.get("status") in {"failed", "interrupted"}:
                return "failed", latest
        return "unknown", latest

    async def uncertain(self, thread, generation, baseline):
        self.note("reconciling", thread=thread)
        for attempt in range(self.retry_attempts + 1):
            if generation != self.user_generation:
                return
            outcome, turn = await self.reconcile(thread, baseline)
            if outcome in {"running", "completed"}:
                self.clear_review(thread)
                if outcome == "completed":
                    self.note("completed", thread=thread)
                return
            if outcome == "failed":
                self.clear_review(thread)
                code = (turn.get("error") or {}).get("codexErrorInfo")
                kind = failure_kind(code, (turn.get("error") or {}).get("message"))
                if kind:
                    return await self.recover(thread, generation, kind)
                self.note(
                    "needs_attention", thread=thread, reason="accepted_turn_failed"
                )
                return
            if (
                getattr(getattr(self.backend, "proc", None), "returncode", None)
                is not None
                and self.backend_factory
            ):
                await self.restart_backend()
                return
            if attempt < self.retry_attempts:
                await self.wait_until(self.clock() + self.retry_interval, generation)
        self.hold_for_review(thread)
        self.note("needs_attention", thread=thread, reason="acceptance_unknown")
        self.report(
            "Could not prove the request was accepted. It was not replayed. Use retry after reviewing the saved thread."
        )

    async def wait_until(self, deadline, generation, *, allow_early=True):
        if self.pool:
            self.pool.touch(next_wake=deadline)
        # Injected sleeps keep deterministic test clocks working.
        if self.sleep is not asyncio.sleep:
            return await self.sleep(max(0, deadline - self.clock()))
        while (
            generation == self.user_generation
            and self.attached()
            and self.clock() < deadline
        ):
            self.wake.clear()
            try:
                await asyncio.wait_for(
                    self.wake.wait(), min(30, max(0.01, deadline - self.clock()))
                )
            except asyncio.TimeoutError:
                continue
            if allow_early or self.force_retry.is_set():
                self.force_retry.clear()
                break

    async def maintain(self):
        revision = self.pool.revision() if self.pool else 0
        last_watch = 0
        last_heartbeat = 0
        account_revision = self.pool.revision("accounts") if self.pool else 0
        last_tick = time.time()
        last_network = 0
        while not self.stopping.is_set():
            now = time.time()
            if now - last_tick > 60 or now < last_tick:
                self.wake.set()
            last_tick = now
            if self.pool:
                if now - last_heartbeat > 10:
                    self.pool.renew_reservation(self.preferences["reservation_seconds"])
                    self.pool.touch(
                        threads=list(self.last_turn),
                        model=next((self.model_for(t) for t in self.last_turn), None),
                        paused=self.paused,
                        background=self.background,
                    )
                    last_heartbeat = now
                new_revision = self.pool.revision()
                if new_revision != revision:
                    self.wake.set()
                    revision = new_revision
                    try:
                        self.preferences = settings.load(
                            self.pool.state, self.project, self.profile
                        )
                        self.retry_interval = self.preferences["retry_interval"]
                        self.retry_attempts = self.preferences["retry_attempts"]
                        self.interval = self.preferences["unknown_reset_interval"]
                    except ValueError:
                        self.note("needs_attention", reason="invalid_settings")
                changed = self.pool.revision("accounts")
                if changed != account_revision and self.extra_accounts:
                    self.accounts = await self.extra_accounts(self.accounts)
                    account_revision = changed
                    self.wake.set()
                for command, op, params in self.pool.take_commands():
                    try:
                        await self.control(op, params)
                        self.pool.ack(command, "done")
                    except Exception:
                        self.pool.ack(command, "failed")
                        self.note("control_failed", operation=op)
            if self.preferences.get("keep_awake") and self.active_threads:
                if (
                    self.awake_process is None
                    or self.awake_process.returncode is not None
                ):
                    self.awake_process = await asyncio.create_subprocess_exec(
                        "/usr/bin/caffeinate",
                        "-i",
                        "-w",
                        str(os.getpid()),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
            elif self.awake_process and self.awake_process.returncode is None:
                self.awake_process.terminate()
                await self.awake_process.wait()
                self.awake_process = None
            if (
                self.network_state is False
                and now - last_network >= self.preferences["offline_retry"]
                and hasattr(self.account, "network")
            ):
                last_network = now
                try:
                    self.network_state = await self.account.network()
                    if self.network_state:
                        self.note("connection_restored")
                        if self.recovery and not self.recovery.done():
                            self.force_retry.set()
                        self.wake.set()
                except (RouterError, asyncio.TimeoutError):
                    pass
            if now - last_watch >= self.preferences["watchdog_interval"]:
                last_watch = now
                if self.backend.proc.returncode is not None and self.backend_factory:
                    if not self.restart_task or self.restart_task.done():
                        self.restart_task = asyncio.create_task(self.restart_backend())
                elif self.active_threads:
                    for thread in list(self.active_threads):
                        with contextlib.suppress(RouterError, asyncio.TimeoutError):
                            await self.reconcile(thread)
                    self.status_changed.set()
            try:
                await asyncio.wait_for(self.stopping.wait(), 2)
            except asyncio.TimeoutError:
                pass

    async def control(self, op, params):
        thread = params.get("thread") or next(reversed(self.last_turn), None)
        if op == "pause":
            self.paused = True
            self.note("paused")
        elif op == "resume":
            self.paused = False
            self.wake.set()
            self.note("resumed")
        elif op == "stop":
            self.stopping.set()
        elif op == "detach":
            if self.pool:
                from service import folder
                from settings import atomic_json

                file = folder(self.pool.state) / (self.pool.session + ".job.json")
                if file.exists():
                    spec = json.loads(file.read_text())
                    spec["takeover"] = True
                    atomic_json(file, spec)
            if self.on_detach:
                await self.on_detach()
            if self.client:
                await self.client.close(
                    code=1000, reason="Moving terminal attachment; task continues"
                )
        elif op == "permissions":
            if self.active_threads or self.active_items:
                raise RouterError("Session is busy")
            if thread:
                await self.backend.call("thread/resume", {"threadId": thread, **params})
                self.merge_settings(thread, params)
            self.launch_overrides = {**self.launch_overrides, **params}
            if params.get("permissions"):
                self.launch_overrides.pop("sandbox", None)
            elif params.get("sandbox"):
                self.launch_overrides.pop("permissions", None)
        elif op == "switch":
            label = params.get("account")
            index = next(
                (i for i, a in enumerate(self.accounts) if a.label == label), None
            )
            if index is None:
                raise RouterError("Unknown account")
            if self.active_threads or self.active_items or self.server_requests:
                raise RouterError("Session is busy; switch at an idle boundary")
            options = self.pool.options(label) if self.pool else {}
            model = self.model_for(thread) if thread else None
            if (
                not options.get("enabled", True)
                or options.get("maintenance")
                or options.get("models")
                and model
                and model not in options["models"]
            ):
                raise RouterError(
                    "Account is disabled, under maintenance, or unavailable for this model"
                )
            if limited(await self.accounts[index].usage(force=True), model):
                raise RouterError("Account allowance is exhausted")
            if self.pool and not self.pool.reserve(
                label,
                limit=options.get("max_sessions")
                or self.preferences["max_sessions_per_account"],
            ):
                raise RouterError("Account capacity is full or maintenance has started")
            try:
                await self.login(index)
            finally:
                if self.pool:
                    self.pool.release_reservation()
            self.note("switched", to=label)
        elif op == "retry":
            self.paused = False
            if thread:
                self.clear_review(thread)
            self.wake.set()
            if self.recovery and not self.recovery.done():
                self.force_retry.set()
            else:
                self.force_retry.clear()
            if self.pool:
                self.pool.clear(
                    self.account.label, self.model_for(thread) if thread else None
                )
                self.pool.invalidate(self.account.label)
            if thread and (not self.recovery or self.recovery.done()):
                self.recovery = asyncio.create_task(
                    self.continue_thread(thread, self.user_generation)
                )
        elif op == "wake":
            self.wake.set()
        else:
            raise RouterError("Unknown control operation")
        if self.pool:
            self.pool.touch(paused=self.paused)
        self.status_changed.set()

    async def restart_backend(self):
        async with self.restart_lock:
            if self.backend.proc.returncode is None:
                return
            self.note("restarting")
            in_flight = bool(self.active_items or self.server_requests)
            old = self.backend
            candidate = None
            try:
                candidate = await self.backend_factory()
                candidate.handler = self.event
                await candidate.initialize()
                self.backend = candidate
                await self.login(self.index)
            except Exception:
                self.backend = old
                if candidate:
                    await candidate.close()
                self.note("backend_restart_failed", reason="startup_or_login_failed")
                return
            self.active_threads.clear()
            self.server_requests.clear()
            self.pending_server_requests.clear()
            self.replayed_requests.clear()
            self.active_items.clear()
            for thread in list(self.last_turn):
                params = {"threadId": thread}
                params.update(
                    {
                        k: v
                        for k, v in self.thread_settings.get(thread, {}).items()
                        if k in RESUME_FIELDS
                    }
                )
                if self.launch_overrides.get("config"):
                    params["config"] = self.launch_overrides["config"]
                try:
                    await self.backend.call("thread/resume", params)
                except (RouterError, asyncio.TimeoutError):
                    self.hold_for_review(thread)
                    self.note(
                        "needs_attention", thread=thread, reason="backend_resume_failed"
                    )
                    continue
                if in_flight:
                    self.hold_for_review(thread)
                    self.note(
                        "needs_attention",
                        thread=thread,
                        reason="action_interrupted_by_crash",
                    )
                else:
                    state, turn = await self.reconcile(thread)
                    persisted = self.pool.intent(thread) if self.pool else {}
                    if (
                        state != "completed"
                        and not self.active_threads
                        and persisted
                        and persisted.get("state") not in {"completed", "idle", None}
                    ):
                        self.recovery = asyncio.create_task(
                            self.restore_recovery(thread, persisted)
                        )
            await old.close()
            if self.paused_uncertain:
                self.note("needs_attention", reason="backend_restored_review_required")
            else:
                self.note("backend_restored")
            self.status_changed.set()

    @property
    def account(self):
        return self.accounts[self.index]

    async def login(self, index):
        # A sent login cannot be undone by cancelling its waiter. Complete the
        # state transition before allowing a new user turn to use this backend.
        task = asyncio.create_task(self._login(index))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _login(self, index):
        async with self.login_lock:
            credentials = await self.accounts[index].credentials()
            await self.backend.call(
                "account/login/start", {"type": "chatgptAuthTokens", **credentials}
            )
            self.index = index
            # Acceptance of token syntax alone isn't a verified, usable login.
            result = await self.backend.call("account/read", {"refreshToken": False})
            if not result.get("account"):
                raise RouterError("The selected account was not confirmed by Codex.")
            if self.pool:
                self.pool.assign(self.account.label)

    async def emit(self, message):
        if self.client:
            try:
                await self.client.send(json.dumps(message))
            except Exception:
                pass

    async def event(self, message):
        method, params = message.get("method"), message.get("params", {})
        if method == "account/chatgptAuthTokens/refresh":
            try:
                # Never return another identity in response to a refresh request.
                credentials = await self.account.credentials(refresh=True)
                previous = params.get("previousAccountId")
                if previous and previous != credentials["chatgptAccountId"]:
                    raise RouterError(
                        "Refresh account mismatch; re-enroll the account."
                    )
                await self.backend.send({"id": message["id"], "result": credentials})
            except (RouterError, asyncio.TimeoutError):
                await self.backend.send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32000,
                            "message": "Account requires sign-in.",
                        },
                    }
                )
            return
        if "id" in message and method:
            self.server_requests.add(message["id"])
            self.pending_server_requests[message["id"]] = message
            if not self.client:
                self.note(
                    "needs_attention",
                    reason="approval_or_tool_response_required",
                    thread=params.get("threadId"),
                )
        if method == "serverRequest/resolved":
            self.server_requests.discard(params.get("requestId"))
            self.pending_server_requests.pop(params.get("requestId"), None)
        if method == "thread/started":
            thread = params.get("thread", {})
            if self.pool and thread.get("id"):
                self.pool.claim(thread["id"])
        if method == "turn/started":
            self.active_threads.add(params["threadId"])
            self.turn_ids[params["threadId"]] = params.get("turn", {}).get("id")
            if params["threadId"] in self.pending_intents:
                self.pending_intents[params["threadId"]]["accepted"] = True
            self.note("working", thread=params["threadId"])
            self.persist(
                params["threadId"],
                accepted=True,
                turn=params.get("turn", {}).get("id"),
                state="working",
            )
        elif method == "item/started" and params.get("item", {}).get("type") in {
            "commandExecution",
            "mcpToolCall",
            "fileChange",
            "dynamicToolCall",
        }:
            self.active_items.add(params["item"]["id"])
            if params.get("threadId"):
                self.persist(params["threadId"], active_items=True)
        elif method == "item/completed":
            self.active_items.discard(params.get("item", {}).get("id"))
            if params.get("threadId"):
                self.persist(params["threadId"], active_items=bool(self.active_items))
        elif method == "model/rerouted":
            if params.get("toModel"):
                self.merge_settings(params["threadId"], {"model": params["toModel"]})
        elif method == "thread/closed":
            self.active_threads.discard(params["threadId"])
        elif method == "thread/status/changed":
            if params.get("status", {}).get("type") == "active":
                self.active_threads.add(params["threadId"])
            else:
                self.active_threads.discard(params["threadId"])
        elif method == "turn/completed":
            thread = params["threadId"]
            self.active_threads.discard(thread)
            turn = params.get("turn", {})
            self.turn_ids[thread] = turn.get("id")
            self.persist(
                thread,
                state=turn.get("status"),
                accepted=True,
                turn=turn.get("id"),
                active_items=bool(self.active_items),
            )
            failure = (turn.get("error") or {}).get("codexErrorInfo")
            key = (thread, turn.get("id"))
            kind = failure_kind(failure, (turn.get("error") or {}).get("message"))
            if failure == "modelNotFound" and self.preferences.get("fallback_models"):
                kind = "model"
            if (
                turn.get("status") == "failed"
                and failure is not None
                and not kind
                and key in self.seen_failures
            ):
                if self.recovery:
                    self.recovery.cancel()
                self.episodes.pop(thread, None)
                self.note(
                    "needs_attention",
                    thread=thread,
                    reason="terminal_error_requires_review",
                )
            if turn.get("status") == "completed":
                if (
                    key in self.seen_failures
                    and self.recovery
                    and self.recovery is not asyncio.current_task()
                ):
                    self.recovery.cancel()
                self.episodes.pop(thread, None)
                self.persist(thread, kind=None, episode=None, next_wake=None)
                self.note("completed", thread=thread)
            if (
                turn.get("status") == "failed"
                and kind
                and key not in self.seen_failures
            ):
                self.seen_failures.add(key)
                if thread in self.last_turn:
                    if self.recovery and not self.recovery.done():
                        self.recovery.cancel()
                    self.recovery = asyncio.create_task(
                        self.recover(thread, self.user_generation, kind)
                    )
        elif method == "error" and params.get("willRetry") is False:
            error = params.get("error") or {}
            thread = params.get("threadId")
            key = (thread, params.get("turnId"))
            if (
                failure_kind(error.get("codexErrorInfo"), error.get("message"))
                == "capacity"
                and thread in self.last_turn
                and key not in self.seen_failures
            ):
                self.seen_failures.add(key)
                if self.recovery and not self.recovery.done():
                    self.recovery.cancel()
                self.recovery = asyncio.create_task(
                    self.recover(thread, self.user_generation, "capacity")
                )
        self.status_changed.set()
        await self.emit(message)

    def model_for(self, thread):
        return self.thread_settings.get(thread, {}).get("model") or self.last_turn.get(
            thread, {}
        ).get("model")

    def deadline(self, index, model):
        return (
            self.pool.deadline(self.accounts[index].label, model)
            if self.pool
            else self.local_deadlines.get(index, 0)
        )

    async def mark(self, index, model, reason="quota"):
        snapshot = None
        if (
            self.pool
            and reason == "quota"
            and self.pool.deadline(self.accounts[index].label, model) > self.clock()
        ):
            return self.pool.deadline(self.accounts[index].label, model)
        if reason == "quota":
            try:
                snapshot = await self.accounts[index].usage(force=True)
            except (RouterError, asyncio.TimeoutError):
                pass
        if self.pool:
            return self.pool.mark(
                self.accounts[index].label,
                model,
                snapshot=snapshot,
                reason=reason,
                delay=self.interval,
            )
        until = (
            reset_time(snapshot or {}, model, self.clock())
            or self.clock() + self.interval
        )
        self.local_deadlines[index] = until
        return until

    async def wait_idle(self, generation):
        began = time.monotonic()
        while (
            self.active_threads
            or self.active_items
            or self.server_requests
            or self.paused
        ):
            if generation != self.user_generation or not self.attached():
                return False
            self.status_changed.clear()
            try:
                await asyncio.wait_for(
                    self.status_changed.wait(), self.preferences["watchdog_interval"]
                )
            except asyncio.TimeoutError:
                for thread in list(self.active_threads):
                    with contextlib.suppress(RouterError, asyncio.TimeoutError):
                        await self.reconcile(thread)
                if time.monotonic() - began > self.preferences["idle_warning_after"]:
                    self.note(
                        "waiting_for_activity", reason="actions_or_approvals_not_idle"
                    )
            if self.paused:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 2)
                except asyncio.TimeoutError:
                    pass
        return generation == self.user_generation and self.attached()

    async def continue_thread(self, thread, generation):
        if not self.can_continue(thread, generation):
            return
        if not await self.wait_idle(generation) or not self.can_continue(
            thread, generation
        ):
            return
        params = {
            "threadId": thread,
            "input": [{"type": "text", "text": CONTINUE, "text_elements": []}],
        }
        for key in ("outputSchema", "cyberAccessProgram", "effort", "summary"):
            if self.thread_settings.get(thread, {}).get(key) is not None:
                params[key] = self.thread_settings[thread][key]
        params["clientUserMessageId"] = str(uuid.uuid4())
        baseline = self.turn_ids.get(thread) or "__unknown_before_submission__"
        self.pending_intents[thread] = {"baseline": baseline, "accepted": False}
        self.persist(thread, accepted=False, state="submitting", baseline=baseline)
        try:
            result = await self.backend.call("turn/start", params)
            turn = result.get("turn", {})
            if turn.get("id"):
                self.turn_ids[thread] = turn["id"]
                self.pending_intents[thread]["accepted"] = True
                self.persist(
                    thread,
                    accepted=True,
                    turn=turn["id"],
                    state=turn.get("status", "working"),
                )
        except RemoteError as error:
            if error.payload.get("code") == -32001:
                return await self.recover(thread, generation, "transient")
            raise
        except (RouterError, asyncio.TimeoutError):
            return await self.uncertain(thread, generation, baseline)
        self.report(f"Continued the saved thread with {self.account_name()}.")

    async def restore_recovery(self, thread, persisted):
        """Honor an accepted retry's original deadline after a process restart."""
        generation = self.user_generation
        if persisted.get("review_required"):
            self.hold_for_review(
                thread, persisted.get("review_reason") or "review_required"
            )
            return
        episode = persisted.get("episode")
        if episode:
            episode = dict(episode)
            label = persisted.get("account_label")
            episode["account"] = next(
                (i for i, a in enumerate(self.accounts) if a.label == label),
                episode.get("account", self.index),
            )
            self.episodes[thread] = episode
        deadline = persisted.get("next_wake")
        if deadline and deadline > self.clock():
            self.note("retry_scheduled", thread=thread, next_wake=deadline)
            await self.wait_until(
                deadline, generation, allow_early=persisted.get("kind") == "quota"
            )
        if persisted.get("active_items") or not persisted.get("accepted", False):
            self.hold_for_review(thread)
            self.note(
                "needs_attention",
                thread=thread,
                reason="worker_restarted_during_uncertain_action",
            )
            return
        if not await self.wait_idle(generation):
            return
        if persisted.get("kind") == "auth":
            try:
                await self.account.credentials(refresh=True)
                await self.login(self.index)
            except (RouterError, asyncio.TimeoutError):
                return await self.recover(thread, generation, "auth")
        await self.continue_thread(thread, generation)

    async def choose(self, model=None, preferred=None):
        """Return (account index or None, earliest next read timestamp)."""
        loads = self.pool.loads() if self.pool else {}
        start = self.index

        def score(i):
            account = self.accounts[i]
            options = self.pool.options(account.label) if self.pool else {}
            snapshot = self.pool.cached(account.label, 3600) if self.pool else None
            remaining = min(
                (
                    100 - (b.get(k) or {}).get("usedPercent", 0)
                    for b in __import__("pool").buckets(snapshot or {}, model)
                    for k in ("primary", "secondary")
                ),
                default=100,
            )
            mode = self.preferences["routing"]
            fav = self.preferences.get("preferred_accounts", [])
            priority = -options.get("priority", 0)
            load = loads.get(account.label, 0)
            if mode == "quota":
                rank = (-remaining, load, priority)
            elif mode == "priority":
                rank = (priority, load, -remaining)
            elif mode == "round-robin":
                rank = ((i - start - 1) % len(self.accounts),)
            else:
                rank = (load, priority, -remaining)
            return (
                0 if i == preferred else 1,
                fav.index(account.label) if account.label in fav else len(fav),
                *rank,
                (i - start - 1) % len(self.accounts),
            )

        order = sorted(range(len(self.accounts)), key=score)
        deadlines = []
        for index in order:
            deadline = self.deadline(index, model)
            if deadline > self.clock():
                deadlines.append(deadline)
                continue
            account = self.accounts[index]
            options = self.pool.options(account.label) if self.pool else {}
            if (
                not options.get("enabled", True)
                or options.get("maintenance")
                or options.get("models")
                and model
                and model not in options["models"]
            ):
                continue
            try:
                # A reset timestamp authorizes a new check, not usage itself.
                snapshot = await account.usage(force=bool(deadline))
                if limited(snapshot, model):
                    if self.pool:
                        until = self.pool.mark(
                            account.label, model, snapshot=snapshot, delay=self.interval
                        )
                    else:
                        until = (
                            reset_time(snapshot, model, self.clock())
                            or self.clock() + self.interval
                        )
                        self.local_deadlines[index] = until
                    deadlines.append(until)
                    continue
                if self.pool:
                    self.pool.clear(account.label, model)
                else:
                    self.local_deadlines.pop(index, None)
                if self.pool and not self.pool.reserve(
                    account.label,
                    expected_load=loads.get(account.label, 0),
                    limit=options.get("max_sessions")
                    or self.preferences["max_sessions_per_account"],
                    ttl=self.preferences["reservation_seconds"],
                ):
                    deadlines.append(self.clock() + 2)
                    continue
                return index, 0
            except (RouterError, asyncio.TimeoutError):
                deadlines.append(await self.mark(index, model, "unavailable"))
        return None, min(deadlines, default=self.clock() + self.interval)

    async def recover(self, thread, generation, kind="quota"):
        if _recovery_active.get() is asyncio.current_task():
            raise RecoveryTransition(kind)
        token = _recovery_active.set(asyncio.current_task())
        try:
            while self.can_continue(thread, generation):
                try:
                    return await self._recover_episode(thread, generation, kind)
                except RecoveryTransition as transition:
                    kind = transition.kind
                    await asyncio.sleep(0)
        finally:
            _recovery_active.reset(token)

    async def _recover_episode(self, thread, generation, kind):
        try:
            model = self.model_for(thread)
            if kind == "model":
                tried = self.fallback_tried.setdefault(thread, set())
                tried.add(model)
                fallback = next(
                    (
                        m
                        for m in self.preferences.get("fallback_models", [])
                        if m not in tried
                    ),
                    None,
                )
                if not fallback:
                    self.note(
                        "needs_attention",
                        thread=thread,
                        reason="configured_model_fallbacks_exhausted",
                    )
                    return
                tried.add(fallback)
                self.merge_settings(thread, {"model": fallback})
                await self.backend.call(
                    "thread/resume", {"threadId": thread, "model": fallback}
                )
                self.note("model_fallback", thread=thread, model=fallback)
                return await self.continue_thread(thread, generation)
            if kind == "transient" and hasattr(self.account, "network"):
                with contextlib.suppress(RouterError, asyncio.TimeoutError):
                    self.network_state = await self.account.network()
                    if self.network_state is False:
                        self.note("connection_probe_failed", thread=thread)
            if kind != "quota":
                episode = self.episodes.setdefault(
                    thread, {"account": self.index, "start": self.clock(), "attempt": 0}
                )
                if episode["account"] != self.index:
                    episode = {
                        "account": self.index,
                        "start": self.clock(),
                        "attempt": 0,
                    }
                    self.episodes[thread] = episode
                if kind == "capacity" and episode["attempt"] >= self.retry_attempts:
                    if self.preferences.get("fallback_models"):
                        return await self.recover(thread, generation, "model")
                    if not self.retry_attempts:
                        self.note(
                            "needs_attention",
                            thread=thread,
                            reason="capacity_retries_disabled",
                        )
                        return
                    # Model-wide capacity does not establish an account problem.
                    # Keep waiting on this account/model instead of cycling logins.
                    episode = {
                        "account": self.index,
                        "start": self.clock(),
                        "attempt": 0,
                    }
                    self.episodes[thread] = episode
                if episode["attempt"] < self.retry_attempts:
                    episode["attempt"] += 1
                    target = episode["start"] + episode["attempt"] * self.retry_interval
                    self.report(
                        f"Service/authentication failure on {self.account_name()}. Retry {episode['attempt']}/{self.retry_attempts} at {time.strftime('%H:%M:%S', time.localtime(target))}."
                    )
                    self.note(
                        "retry_scheduled",
                        thread=thread,
                        next_wake=target,
                        attempt=episode["attempt"],
                    )
                    self.persist(
                        thread,
                        generation=generation,
                        kind=kind,
                        episode=episode,
                        next_wake=target,
                        account_label=self.account.label,
                        state="retry_scheduled",
                    )
                    await self.wait_until(target, generation, allow_early=False)
                    if not await self.wait_idle(generation):
                        return
                    if kind == "auth":
                        try:
                            await self.account.credentials(refresh=True)
                            await self.login(self.index)
                        except (RouterError, asyncio.TimeoutError):
                            # Keep this same schedule even if login is revoked.
                            return await self.recover(thread, generation, kind)
                    return await self.continue_thread(thread, generation)
                self.report(
                    "Three retries over 15 minutes failed; trying another account."
                )
            self.episodes.pop(thread, None)
            self.report(
                f"Selecting another account after {kind} failure on {self.account_name()}."
            )
            self.note("selecting_account", thread=thread, reason=kind)
            await self.mark(self.index, model, kind)
            if kind != "quota" and self.preferences.get("fallback_models"):
                configured = self.preferences["fallback_models"]
                if kind in {"transient", "capacity"} and model not in configured:
                    self.merge_settings(thread, {"model": configured[0]})
                    await self.backend.call(
                        "thread/resume", {"threadId": thread, "model": configured[0]}
                    )
                    model = configured[0]
                    self.note("model_fallback", thread=thread, model=model)
            while generation == self.user_generation and self.attached():
                if not await self.wait_idle(generation):
                    return
                index, deadline = await self.choose(model)
                if index is not None:
                    if not await self.wait_idle(generation):
                        return
                    try:
                        await self.login(index)
                        return await self.continue_thread(thread, generation)
                    except (RouterError, asyncio.TimeoutError):
                        if self.pool:
                            self.pool.release_reservation()
                        await self.mark(index, model, "unavailable")
                        continue
                self.report(
                    f"All accounts unavailable. Sleeping until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(deadline))}; no usage requests before then."
                )
                self.note("waiting_for_reset", thread=thread, next_wake=deadline)
                self.persist(
                    thread,
                    generation=generation,
                    kind=kind,
                    next_wake=deadline,
                    account_label=self.account.label,
                    state="waiting_for_reset",
                )
                await self.wait_until(max(self.clock() + 1, deadline), generation)
        except (asyncio.CancelledError, RecoveryTransition):
            raise
        except Exception:
            self.hold_for_review(thread, "handoff_failed")
            self.note("needs_attention", thread=thread, reason="handoff_failed")
            self.report(
                "Automatic handoff stopped. Your saved thread is retained; continue manually."
            )

    async def forward(self, message, init_result):
        method = message.get("method")
        request_id = message.get("id")
        if (
            method in {"thread/start", "thread/resume", "thread/fork"}
            and self.launch_overrides
        ):
            message = dict(message)
            message["params"] = {**message.get("params", {}), **self.launch_overrides}
        if (
            method in {"thread/start", "thread/resume", "thread/fork"}
            and self.additional_roots
        ):
            message = {**message, "params": dict(message.get("params", {}))}
            params = message["params"]
            params["runtimeWorkspaceRoots"] = list(
                dict.fromkeys(
                    [
                        *(
                            params.get("runtimeWorkspaceRoots")
                            or [params.get("cwd") or self.project]
                        ),
                        *self.additional_roots,
                    ]
                )
            )
        if not method:
            # Approval responses keep the server's original ID, never our call IDs.
            if request_id in self.server_requests:
                self.server_requests.discard(request_id)
                self.pending_server_requests.pop(request_id, None)
                await self.backend.send(message)
            return
        if method == "initialized":
            return
        if method == "initialize":
            await self.emit({"id": request_id, "result": init_result})
            return
        if method.startswith("account/login") or method == "account/logout":
            await self.emit(
                {
                    "id": request_id,
                    "error": {
                        "code": -32600,
                        "message": "Close this controller before managing pool logins with relay add.",
                    },
                }
            )
            return
        if method in {"thread/resume", "turn/start", "turn/steer"}:
            thread = message.get("params", {}).get("threadId")
            if self.pool and thread and not self.pool.claim(thread):
                await self.emit(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32600,
                            "message": "This saved conversation is already open in another wrapper terminal. Use a different thread or close that terminal.",
                        },
                    }
                )
                return
        if method in {
            "turn/start",
            "turn/steer",
            "turn/interrupt",
            "thread/archive",
            "thread/delete",
        }:
            self.user_generation += 1
            if self.recovery and not self.recovery.done():
                self.recovery.cancel()
            self.episodes.clear()
            if method == "turn/start" and message.get("params", {}).get("model"):
                self.fallback_tried.pop(message["params"]["threadId"], None)
            self.status_changed.set()
            if method == "turn/start":
                self.clear_review(message["params"]["threadId"])
                self.last_turn[message["params"]["threadId"]] = message["params"]
                self.merge_settings(message["params"]["threadId"], message["params"])
        try:
            if request_id is None:
                await self.backend.send(message)
            else:
                if method == "turn/start" and self.pool:
                    thread = message["params"]["threadId"]
                    model = self.model_for(thread)
                    allowed = self.pool.options(self.account.label).get("models", [])
                    if allowed and model and model not in allowed:
                        if (
                            self.active_threads
                            or self.active_items
                            or self.server_requests
                        ):
                            raise RemoteError(
                                {
                                    "code": -32600,
                                    "message": "This account does not allow that model. Switch accounts after the current work finishes.",
                                }
                            )
                        index, _ = await self.choose(model)
                        if index is None:
                            raise RemoteError(
                                {
                                    "code": -32600,
                                    "message": "No available account allows that model. Choose another model or update your account model settings.",
                                }
                            )
                        await self.login(index)
                        self.note("switched", to=self.account.label)
                guard = (
                    self.login_lock
                    if method
                    in {
                        "turn/start",
                        "turn/steer",
                        "turn/interrupt",
                        "thread/start",
                        "thread/resume",
                    }
                    else contextlib.nullcontext()
                )
                if method == "turn/start":
                    thread = message["params"]["threadId"]
                    if thread not in self.turn_ids:
                        try:
                            latest = await self.latest(thread)
                            self.turn_ids[thread] = latest.get("id") if latest else None
                        except (RouterError, asyncio.TimeoutError):
                            self.turn_ids[thread] = "__unknown_before_submission__"
                    message["params"].setdefault(
                        "clientUserMessageId", str(uuid.uuid4())
                    )
                    self.pending_intents[thread] = {
                        "baseline": self.turn_ids.get(thread),
                        "generation": self.user_generation,
                    }
                    self.persist(
                        thread,
                        accepted=False,
                        baseline=self.turn_ids.get(thread),
                        state="submitting",
                    )
                async with guard:
                    result = await self.backend.call(
                        method, message.get("params"), timeout=300
                    )
                if method == "turn/start" and result.get("turn", {}).get("id"):
                    thread = message["params"]["threadId"]
                    self.turn_ids[thread] = result["turn"]["id"]
                    self.pending_intents[thread]["accepted"] = True
                    self.persist(
                        thread,
                        accepted=True,
                        turn=result["turn"]["id"],
                        state=result["turn"].get("status"),
                    )
                if method in {"thread/start", "thread/resume", "thread/fork"}:
                    thread = result.get("thread", {})
                    if thread.get("id"):
                        if thread.get("canAcceptDirectInput") is False:
                            if self.pool:
                                self.pool.release_thread(thread["id"])
                            self.report(
                                "This conversation is owned by another Codex backend. Keep using its existing terminal, or fork it into a separate conversation. No running task was stopped."
                            )
                            await self.emit(
                                {
                                    "id": request_id,
                                    "error": {
                                        "code": -32600,
                                        "message": "Conversation is open in another Codex backend. Close/unload it there before resuming here, or use relay run -- fork "
                                        + thread["id"]
                                        + " --yolo for an independent copy. No running task was stopped.",
                                    },
                                }
                            )
                            return
                        self.merge_settings(thread["id"], result)
                        self.merge_settings(thread["id"], message.get("params", {}))
                        self.last_turn.setdefault(
                            thread["id"], {"threadId": thread["id"]}
                        )
                        self.persist(thread["id"], role="foreground")
                        if self.on_thread:
                            await self.on_thread(
                                thread["id"], self.thread_settings[thread["id"]]
                            )
                        if self.pool:
                            self.pool.claim(thread["id"])
                            self.pool.touch(
                                thread=thread["id"],
                                cwd=result.get("cwd") or thread.get("cwd"),
                                model=self.model_for(thread["id"]),
                            )
                await self.emit({"id": request_id, "result": result})
                if method in {"thread/start", "thread/resume", "thread/fork"}:
                    thread_id = result.get("thread", {}).get("id")
                    for ident, pending in list(self.pending_server_requests.items()):
                        if ident not in self.replayed_requests and pending.get(
                            "params", {}
                        ).get("threadId") in {None, thread_id}:
                            await self.emit(pending)
                            self.replayed_requests.add(ident)
        except RemoteError as error:
            await self.emit({"id": request_id, "error": error.payload})
            # Ingress overload rejects a turn before a turn/completed event
            # exists. Its documented code is explicit, not message matching.
            if method == "turn/start" and error.payload.get("code") == -32001:
                if self.recovery and not self.recovery.done():
                    self.recovery.cancel()
                thread = message["params"]["threadId"]
                self.recovery = asyncio.create_task(
                    self.recover(thread, self.user_generation, "transient")
                )
        except (RouterError, asyncio.TimeoutError):
            await self.emit(
                {
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": "Codex request failed. No automatic retry of this action was made.",
                    },
                }
            )
            if method == "turn/start":
                thread = message["params"]["threadId"]
                intent = self.pending_intents.get(thread, {})
                if self.recovery and not self.recovery.done():
                    self.recovery.cancel()
                self.recovery = asyncio.create_task(
                    self.uncertain(thread, self.user_generation, intent.get("baseline"))
                )

    async def connect(self, websocket, init_result):
        if self.client:
            await websocket.close(code=1008, reason="One terminal per controller.")
            return
        self.client = websocket
        self.replayed_requests.clear()
        if self.pool:
            self.pool.touch(attached=True)
        try:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError()
                except ValueError:
                    await websocket.close(code=1008, reason="Invalid RPC message.")
                    break
                # Keep notifications ordered; independent long RPC calls must not
                # block approval responses or interruption requests.
                task = asyncio.create_task(self.forward(message, init_result))
                self.client_tasks.add(task)
                task.add_done_callback(self.client_tasks.discard)
        finally:
            self.client = None
            if self.pool:
                self.pool.touch(attached=False)
            if not self.background:
                self.user_generation += 1
                if self.recovery:
                    self.recovery.cancel()
            for task in list(self.client_tasks):
                task.cancel()


async def run_pool(args, state, binary):
    import service

    from launch import frontend_arguments, working_directory, prepare_worktree

    invocation_cwd = os.getcwd()
    cwd = working_directory(args.codex_args, invocation_cwd)
    settings.load(state, cwd, args.profile)
    arguments = frontend_arguments(args.codex_args, invocation_cwd)
    arguments, cwd = prepare_worktree(arguments, state, cwd)
    frontend, overrides = translate(arguments)
    target = await resolve_resume_target(
        frontend,
        Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
        cwd,
        binary,
    )
    pool = Pool(state)
    try:
        owner = pool.owner(target) if target else None
    finally:
        pool.close()
    if owner:
        print(
            "Reconnecting to existing wrapper session " + owner[:10] + ".", flush=True
        )
        return await service.attach(
            state, owner, binary, arguments, wait_forever=True, cwd=invocation_cwd
        )
    ident = service.start(
        state,
        name=args.name or Path(cwd).name,
        cwd=cwd,
        first=args.first,
        arguments=arguments,
        profile=args.profile,
        interactive=True,
        binary=binary,
    )
    try:
        return await service.attach(
            state, ident, binary, arguments, wait_forever=True, cwd=invocation_cwd
        )
    finally:
        # A foreground launch normally closes its worker after the terminal exits.
        # Background mode and explicit takeover preserve the supervised worker.
        spec_path = service.folder(state) / (ident + ".job.json")
        spec = json.loads(spec_path.read_text()) if spec_path.exists() else {}
        current_preferences = settings.load(state, cwd, args.profile)
        if not current_preferences.get("background_on_disconnect") and not spec.get(
            "takeover"
        ):
            pool = Pool(state)
            pool.command(ident, "stop")
            pool.close()
            deadline = time.monotonic() + 10
            while (
                service.folder(state) / (ident + ".connection.json")
            ).exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)


def resume_selection(arguments):
    from launch import tokens

    parsed = list(tokens(arguments))
    positionals = [
        value for name, value, raw in parsed if name is None and len(raw) == 1
    ]
    if not positionals or positionals[0] not in {"resume", "fork"}:
        return None, set()
    flags = {name for name, _, _ in parsed}
    return positionals[1] if len(positionals) > 1 else None, flags


def resume_target(arguments, home=None, cwd=None):
    """Explicit IDs need no native database or backend."""
    selected, _ = resume_selection(arguments)
    try:
        return str(uuid.UUID(selected)) if selected else None
    except (ValueError, AttributeError):
        return None


async def resolve_resume_target(arguments, home, cwd, binary=None, backend=None):
    selected, flags = resume_selection(arguments)
    explicit = resume_target(arguments)
    if explicit or not selected and "--last" not in flags:
        return explicit
    own_backend = backend is None
    if own_backend:
        backend = await RPC.start(binary, home)
        await backend.initialize()
    try:
        params = {
            "archived": False,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "limit": 100,
        }
        if "--all" not in flags:
            params["cwd"] = cwd
        if "--include-non-interactive" in flags:
            params["sourceKinds"] = [
                "cli",
                "vscode",
                "exec",
                "appServer",
                "subAgent",
                "subAgentReview",
                "subAgentCompact",
                "subAgentThreadSpawn",
                "subAgentOther",
                "unknown",
            ]
        if selected:
            params["searchTerm"] = selected
        while True:
            response = await backend.call("thread/list", params)
            for thread in response.get("data", []):
                if not selected or selected in {
                    thread.get("name"),
                    thread.get("title"),
                    thread.get("id"),
                }:
                    return thread["id"]
            cursor = response.get("nextCursor")
            if not cursor:
                return None
            params["cursor"] = cursor
    finally:
        if own_backend:
            await backend.close()


def add_account(args, state, binary):
    home = private_dir(account_home(state, args.label))
    old_identity = (
        json.loads((home / "enrolled").read_text()).get("identity")
        if (home / "enrolled").exists()
        else None
    )
    # Only these isolated homes use the direct Keychain layout. Existing Codex
    # settings, login entries, session files and binaries are never replaced.
    config = home / "config.toml"
    config.write_text(
        'cli_auth_credentials_store = "keyring"\n[features]\nsecret_auth_storage = false\n[analytics]\nenabled = false\n'
    )
    config.chmod(0o600)
    command = [binary, "login"] + (["--device-auth"] if args.device_auth else [])
    print(f"Sign into the ChatGPT account you want to label {args.label}.")
    result = subprocess.run(command, env=clean_env(home))
    if result.returncode:
        raise RouterError("Sign-in did not finish. No account was added to the pool.")
    # Once native login changes this home's credential, an old marker must no
    # longer make a failed/duplicate enrollment selectable.
    (home / "enrolled").unlink(missing_ok=True)
    credentials = read_keychain(home, include_identity=True)
    identity = hashlib.sha256(credentials["chatgptAccountId"].encode()).hexdigest()
    for label in labels(state):
        if label == args.label:
            continue
        other = json.loads((account_home(state, label) / "enrolled").read_text())
        if identity == other.get("identity"):
            raise RouterError(
                "That account is already enrolled under another label. Select a different browser account and try again."
            )
    marker = home / "enrolled"
    marker.write_text(json.dumps({"identity": identity}) + "\n")
    marker.chmod(0o600)
    if credentials.get("email"):
        from settings import atomic_json

        atomic_json(
            home / "identity.json",
            {"email": credentials["email"], "identity": identity},
        )
    pool = Pool(state)
    if old_identity != identity:
        pool.reset_identity(args.label)
    else:
        pool.invalidate(args.label)
        with pool.db() as db:
            db.execute(
                "DELETE FROM blocks WHERE label=? AND reason<>'quota'", (args.label,)
            )
    pool.set_options(args.label, enabled=True)
    pool.close()
    print(
        f"Saved {args.label} in macOS Keychain. No credentials were printed or saved in the project."
    )


def transport_guard(token):
    async def authenticate(connection, request):
        # Browser origins are rejected even if they present a transport token.
        expected = token() if callable(token) else token
        if (
            request.headers.get("Origin")
            or request.headers.get("Authorization") != f"Bearer {expected}"
        ):
            return connection.respond(403, "Forbidden\n")

    return authenticate


def parser():
    p = argparse.ArgumentParser(
        prog="relay", description="Keep Codex moving between your own accounts (macOS)."
    )
    p.add_argument("--version", action="version", version="Relay " + VERSION)
    sub = p.add_subparsers(dest="command", required=True)
    help_command = sub.add_parser(
        "help", help="Show Relay help or help for one command"
    )
    help_command.add_argument("topic", nargs="*")
    add = sub.add_parser("add", help="Sign into one account using native Codex login")
    add.add_argument("label")
    add.add_argument("--device-auth", action="store_true")
    setup = sub.add_parser(
        "setup", help="Enroll any number of accounts using native browser sign-in"
    )
    setup.add_argument("--device-auth", action="store_true")
    setup.add_argument("labels", nargs="*")
    setup.add_argument("--count", type=int)
    sub.add_parser("list", help="List enrolled labels; never show credentials")
    run = sub.add_parser("run", help="Open Codex with automatic account recovery")
    run.add_argument("--first", help="Account label to start with")
    run.add_argument(
        "--profile", help="Recovery profile, separate from the native Codex profile"
    )
    run.add_argument("--name", help="Friendly session name")
    run.add_argument(
        "--require-recovery",
        action="store_true",
        help="Fail rather than run a native mode outside Relay recovery",
    )
    run.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="Arguments after -- are forwarded to Codex",
    )
    sub.add_parser(
        "check",
        help="Check CLI version, dependency and account count without signing in",
    )
    sub.add_parser(
        "refresh",
        help="Fetch current allowance and reset times, even for cached exhausted accounts",
    )
    import commands

    commands.add_parsers(sub)
    sub.metavar = "COMMAND"
    sub._choices_actions[:] = [
        item for item in sub._choices_actions if not item.dest.startswith("_")
    ]
    return p


def parse_arguments(argv):
    argv = list(argv)
    if argv[:1] == ["attach"]:
        boundary = argv.index("--") if "--" in argv else len(argv)
        if "--takeover" in argv[1:boundary]:
            argv.remove("--takeover")
            argv.insert(1, "--takeover")
    return parser().parse_args(argv)


async def dispatch(args, state, binary):
    import commands

    if args.command == "run":
        from launch import arguments_only, native_mode

        if native_mode(args.codex_args, binary):
            if getattr(args, "require_recovery", False):
                raise RouterError(
                    "This mode runs in native Codex outside Relay recovery. Use relay explain -- to inspect the launch."
                )
            if sys.stderr.isatty():
                print(
                    "Running native Codex; automatic account recovery is inactive for this mode.",
                    file=sys.stderr,
                )
            process = await asyncio.create_subprocess_exec(
                binary, *arguments_only(args.codex_args)
            )
            return await process.wait()

    if args.command == "_broker":
        import broker

        return await broker.serve(state, args.binary or binary)
    if args.command == "_supervise":
        import service

        return await service.supervise(state, args.session)
    if args.command == "_serve":
        import service

        return await service.serve(state, args.session, binary)
    if args.command not in {
        "run",
        "start",
        "attach",
        "add",
        "setup",
        "check",
        "refresh",
    }:
        result = await commands.dispatch(args, state, binary)
        if result is not None:
            return result
    async with file_lock(state / "compatibility.lock"):
        try:
            import runtime

            binary, result, runtime_mode = await asyncio.to_thread(
                runtime.select, binary, state
            )
        except (ValueError, OSError, KeyError, subprocess.SubprocessError):
            raise RouterError(
                "Installed Codex does not expose a compatible protocol. Ordinary codex still works; the wrapper needs an adapter update."
            ) from None
    if args.command == "check":
        print(
            f"{result['version']}; {len(labels(state))} accounts enrolled; capability checks passed."
        )
        print(
            "Parallel terminals enabled; 5/10/15-minute retries; reset-aware scheduling."
        )
        print("Native runtime: " + runtime_mode)
        return 0
    if args.command in {"start", "attach"}:
        return await commands.dispatch(args, state, binary)
    if args.command == "refresh":
        async with file_lock(state / "enrollment.lock", shared=True):
            pool = Pool(state)
            from broker import ClientAccount

            accounts = [
                ClientAccount(label, account_home(state, label), binary, pool)
                for label in labels(state)
            ]
            try:

                async def refresh(account):
                    try:
                        snapshot = await account.usage(force=True)
                        with pool.db() as db:
                            scopes = [
                                r[0]
                                for r in db.execute(
                                    "SELECT scope FROM blocks WHERE label=?",
                                    (account.label,),
                                )
                            ]
                        for scope in set(scopes + ["*"]):
                            model = None if scope == "*" else scope
                            if limited(snapshot, model):
                                pool.mark(account.label, model, snapshot=snapshot)
                            else:
                                pool.clear(account.label, model)
                        print(
                            f"{account.label}: allowance and reset times refreshed.",
                            flush=True,
                        )
                    except (RouterError, asyncio.TimeoutError):
                        print(
                            f"{account.label}: refresh unavailable; existing reset schedule retained.",
                            flush=True,
                        )

                await asyncio.gather(*(refresh(a) for a in accounts))
            finally:
                await asyncio.gather(
                    *(a.close() for a in accounts), return_exceptions=True
                )
                pool.close()
            pool = Pool(state)
            pool.signal()
            pool.close()
        return 0
    if args.command in {"add", "setup"}:
        if args.command == "setup":
            chosen = list(args.labels)
            if args.count is not None:
                if args.count < 1:
                    raise RouterError("Account count must be at least 1")
                chosen.extend(
                    "account" + str(i)
                    for i in range(1, args.count + 1)
                    if "account" + str(i) not in labels(state)
                )
            elif not chosen and not labels(state):
                chosen = ["account1"]
            if not chosen:
                print(
                    f"{len(labels(state))} accounts enrolled. Use setup --count N or add LABEL to add more."
                )
                return 0
            for label in dict.fromkeys(chosen):
                import argparse

                request = argparse.Namespace(
                    action="add", label=label, device_auth=args.device_auth
                )
                await commands.manage(request, state, binary)
            return 0
        args.action = "add"
        return await commands.manage(args, state, binary)
    if args.first:
        args.first = commands.resolve_label(state, args.first)
    import execution

    if execution.command(args.codex_args):
        return await execution.run(args, state, binary)
    return await run_pool(args, state, binary)


def selected_binary(args, state):
    if args.command == "_broker" and args.binary:
        return args.binary
    if (
        args.command == "runtime"
        and args.action in {"cache", "preflight"}
        and args.value
    ):
        return args.value
    if args.command == "_serve":
        job = state / "services" / (args.session + ".job.json")
        if job.exists():
            binary = json.loads(job.read_text()).get("binary")
            if binary:
                return binary
    if args.command in {
        "list",
        "status",
        "events",
        "usage",
        "config",
        "pause",
        "unpause",
        "retry",
        "wake",
        "stop",
        "switch",
        "restart",
        "menubar",
        "_supervise",
        "diagnostics",
        "clean",
        "workspace",
        "explain",
        "startup",
    }:
        return None
    if args.command == "group" and args.action != "start":
        return None
    if args.command == "runtime" and args.action in {"list", "use"}:
        return None
    if (
        args.command == "account"
        and args.action not in {"add", "reauth"}
        and not getattr(args, "revoke", False)
    ):
        return None
    try:
        return codex_path()
    except RouterError:
        import compatibility
        import runtime

        for row in reversed(runtime.index(state)["runtimes"]):
            try:
                if compatibility.digest(row["binary"]) == row["sha256"]:
                    return row["binary"]
            except OSError:
                continue
        raise


def main():
    os.umask(0o077)
    argv = sys.argv[1:]
    if argv[:2] == ["help", "run"]:
        argv = ["run", "--help"]
    before_separator = argv[: argv.index("--")] if "--" in argv else argv
    if argv[:1] == ["run"] and any(
        flag in before_separator for flag in ("--help", "-h")
    ):
        result = subprocess.call([codex_path(), "--help"])
        print(
            "\nRelay launch options (before --):\n"
            "  --first ACCOUNT       Start with an enrolled account\n"
            "  --profile NAME        Select a Relay recovery profile\n"
            "  --name NAME           Name this Relay session\n"
            "  --require-recovery    Refuse modes outside account recovery\n\n"
            "Examples:\n  relay run -- resume --yolo\n"
            "  relay run -- -m MODEL -C PATH\n"
            '  relay run -- exec --json "Run the checks"\n\n'
            "Native Codex flags go after --. For native command help:\n"
            "  relay run -- exec --help\n  relay help codex resume"
        )
        return result
    if not argv or argv[:1] == ["help"]:
        topic = argv[1:] if argv else []
        if topic[:1] == ["codex"]:
            return subprocess.call([codex_path(), *topic[1:], "--help"])
        return parser().parse_args([*topic, "--help"])
    args = parse_arguments(argv)
    if args.command in {"run", "attach"}:

        def interrupted(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
    if sys.platform != "darwin":
        raise RouterError(
            "This build uses macOS Keychain and currently supports macOS only."
        )
    state = private_dir(STATE)
    private_dir(state / "accounts")
    binary = selected_binary(args, state)
    if args.command == "list":
        import commands

        value = commands.snapshot(state)
        print(
            "\n".join(
                f"{a['id']}: {a.get('email') or a['name']}" for a in value["accounts"]
            )
            or "No accounts enrolled."
        )
        return 0
    return asyncio.run(dispatch(args, state, binary))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (RouterError, ValueError, asyncio.TimeoutError) as error:
        print(
            f"relay: {error if isinstance(error, (RouterError, ValueError)) else 'Codex operation timed out.'}",
            file=sys.stderr,
        )
        sys.exit(1)
