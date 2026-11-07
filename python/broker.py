"""Private Unix-socket credential authority. Tokens never enter the pool database."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import base64
import hashlib

from pool import file_lock, Pool
from settings import atomic_json
from processes import spawn

LEGACY_CLIENT_REVISION = (
    "b3b3a2c9e0da6e29ce54d2e1b4b747b4372b31d4eb193c0626eee6a86421c3f7"
)
PROTOCOL_VERSION = 1
SOURCE_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class CoordinatorError(RuntimeError):
    def __init__(self, reason=None):
        super().__init__("Credential operation unavailable")
        self.reason = reason


class CredentialCache:
    """Share already-authorized access tokens in RAM, never in a cache file."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.values = {}

    def put(self, label, generation, credentials):
        part = credentials["accessToken"].split(".")[1]
        expiry = json.loads(
            base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        ).get("exp", 0)
        self.values[label] = (generation, expiry, dict(credentials))

    def get(self, label, generation):
        value = self.values.get(label)
        if value and value[0] == generation and value[1] > self.clock() + 120:
            return dict(value[2])
        self.discard(label)
        return None

    def discard(self, label):
        self.values.pop(label, None)


async def native_credentials(account, stats=None):
    """Prefer the signed native helper; keep direct Keychain as a fallback.

    This is a legacy native endpoint, so capability failure must not pin Relay
    to one Codex version. Exported identity must match enrollment before use.
    """
    from router import RemoteError, read_keychain

    await account.start()
    try:
        status = await account.rpc.call(
            "getAuthStatus", {"includeToken": True, "refreshToken": False}
        )
    except RemoteError as error:
        if error.payload.get("code") not in {-32601, -32600}:
            raise
        status = {}
    token = status.get("authToken")
    if status.get("authMethod") in {"chatgpt", "chatgptAuthTokens"} and isinstance(
        token, str
    ):
        try:
            part = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            metadata = claims.get("https://api.openai.com/auth", {})
            account_id = metadata.get("chatgpt_account_id")
            enrolled = json.loads((account.home / "enrolled").read_text()).get(
                "identity"
            )
            if (
                isinstance(account_id, str)
                and hashlib.sha256(account_id.encode()).hexdigest() == enrolled
                and claims.get("exp", 0) > time.time() + 10
            ):
                if stats is not None:
                    stats["native_exports"] += 1
                return {
                    "accessToken": token,
                    "chatgptAccountId": account_id,
                    "chatgptPlanType": metadata.get("chatgpt_plan_type"),
                }
        except (ValueError, TypeError, IndexError, OSError):
            pass
    if stats is not None:
        stats["keychain_fallbacks"] += 1
    return await asyncio.to_thread(read_keychain, account.home)


def paths(state):
    # macOS Unix socket paths have a short length limit.
    import hashlib

    name = hashlib.sha256(str(state.resolve()).encode()).hexdigest()[:20]
    root = Path("/private/tmp") / f"codex-accounts-{os.getuid()}-{name}"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RuntimeError("Unsafe local socket directory")
    root.chmod(0o700)
    return root / "broker.sock", state / "broker.json"


async def exchange(state, request, timeout=60):
    socket, meta = paths(state)
    data = json.loads(meta.read_text())

    async def call():
        reader, writer = await asyncio.open_unix_connection(
            str(socket), limit=4 * 1024 * 1024
        )
        try:
            writer.write(
                json.dumps({"token": data["token"], **request}).encode() + b"\n"
            )
            await writer.drain()
            response = json.loads(await reader.readline())
            if not response.get("ok"):
                raise CoordinatorError(response.get("reason"))
            return response.get("result")
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.wait_for(call(), timeout)


async def ensure(state, binary):
    async with file_lock(state / "broker-start.lock"):
        try:
            running = await exchange(state, {"op": "ping"}, 3)
            if running.get("protocol", 1) == PROTOCOL_VERSION:
                return
            await exchange(state, {"op": "stop"}, 3)
            for _ in range(50):
                if not paths(state)[1].exists():
                    break
                await asyncio.sleep(0.1)
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
            pass
        spawn(
            [
                sys.executable,
                str(Path(__file__).with_name("router.py")),
                "_broker",
                "--binary",
                binary,
            ],
            env={
                **os.environ,
                "CODEX_ACCOUNTS_STATE": str(state),
                "RELAY_STATE": str(state),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                await exchange(state, {"op": "ping"}, 2)
                return
            except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
                await asyncio.sleep(0.1)
        raise RuntimeError("Local credential coordinator did not start")


class ClientAccount:
    def __init__(self, label, home, binary, pool):
        self.label, self.home, self.binary, self.pool = label, home, binary, pool

    async def call(self, op, **params):
        from router import RouterError, SignInRequired, KeychainAccessRequired

        try:
            await ensure(self.pool.state, self.binary)
            return await exchange(
                self.pool.state,
                {"op": op, "label": self.label, **params},
                timeout=90 if op == "credentials" else 60,
            )
        except CoordinatorError as error:
            if error.reason == "sign_in_required":
                raise SignInRequired() from None
            if error.reason == "keychain_access_required":
                raise KeychainAccessRequired() from None
            raise RouterError(
                "Account coordinator could not complete this operation. Check relay doctor."
            ) from None
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
            raise RouterError(
                "Account coordinator could not complete this operation. Check doctor or reauthenticate this account."
            ) from None

    async def credentials(self, refresh=False):
        return await self.call("credentials", refresh=refresh)

    async def usage(self, force=False):
        return await self.call(
            "usage", force=force, after=time.time() if force else None
        )

    async def network(self):
        return await self.call("network")

    async def close(self):
        pass


async def serve(state, binary):
    from router import (
        Account,
        RemoteError,
        SignInRequired,
        KeychainAccessRequired,
        account_home,
        labels,
    )
    import account_auth

    socket, meta = paths(state)
    async with file_lock(state / "broker-owner.lock"):
        socket.unlink(missing_ok=True)
        token = secrets.token_urlsafe(32)
        atomic_json(meta, {"token": token, "pid": os.getpid()})
        pool = Pool(state)
        accounts = {}
        locks = {}
        stamps = {}
        refreshed = {}
        credentials_cache = CredentialCache()
        stats = {
            "native_exports": 0,
            "keychain_fallbacks": 0,
            "credential_cache_hits": 0,
        }
        permission_failures = {}
        last_used = time.monotonic()
        network = {"at": 0, "value": None}
        stop = asyncio.Event()

        async def handle(reader, writer):
            nonlocal last_used
            response = {"ok": False}
            account = None
            stamp = None
            try:
                raw = await asyncio.wait_for(reader.readline(), 5)
                request = json.loads(raw)
                if not secrets.compare_digest(str(request.get("token", "")), token):
                    return
                last_used = time.monotonic()
                op = request.get("op")
                if op == "ping":
                    response = {
                        "ok": True,
                        "result": {
                            "pid": os.getpid(),
                            "revision": LEGACY_CLIENT_REVISION,
                            "protocol": PROTOCOL_VERSION,
                            "build": SOURCE_REVISION,
                            "credential_reads": dict(stats),
                        },
                    }
                elif op == "stop":
                    response = {"ok": True, "result": True}
                    stop.set()
                elif op == "network":
                    if time.monotonic() - network["at"] > 30:
                        try:
                            _, connection = await asyncio.wait_for(
                                asyncio.open_connection("auth.openai.com", 443), 3
                            )
                            connection.close()
                            await connection.wait_closed()
                            network["value"] = True
                        except (OSError, asyncio.TimeoutError):
                            network["value"] = False
                        network["at"] = time.monotonic()
                    response = {"ok": True, "result": network["value"]}
                else:
                    label = request.get("label", "")
                    if label not in labels(state):
                        raise ValueError()
                    if pool.options(label).get("maintenance") and op != "invalidate":
                        raise ValueError()
                    lock = locks.setdefault(label, asyncio.Lock())
                    async with lock:
                        # Enrollment writes a new generation marker and invalidates
                        # this authority before any cached manager is reused.
                        marker = account_home(state, label) / "enrolled"
                        stamp = marker.stat().st_mtime_ns
                        if label in accounts and stamps.get(label) != stamp:
                            await accounts.pop(label).close()
                        if stamps.get(label) != stamp:
                            credentials_cache.discard(label)
                            permission_failures.pop(label, None)
                            refreshed.pop(label, None)
                        account = accounts.setdefault(
                            label,
                            Account(label, account_home(state, label), binary, pool),
                        )
                        stamps[label] = stamp
                        if op != "invalidate" and account_auth.required(account.home):
                            raise SignInRequired()
                        async with file_lock(account.home / "credential.lock"):
                            if op == "credentials":
                                force = bool(request.get("refresh"))
                                value = credentials_cache.get(label, stamp)
                                if (
                                    force
                                    and time.monotonic() - refreshed.get(label, 0) >= 30
                                ):
                                    value = None
                                if value is not None:
                                    stats["credential_cache_hits"] += 1
                                if (
                                    value is None
                                    and time.monotonic()
                                    - max(
                                        permission_failures.values(),
                                        default=float("-inf"),
                                    )
                                    < 60
                                ):
                                    raise KeychainAccessRequired()
                                if value is None and not force:
                                    try:
                                        candidate = await native_credentials(
                                            account, stats
                                        )
                                        credentials_cache.put(label, stamp, candidate)
                                        value = credentials_cache.get(label, stamp)
                                    except KeychainAccessRequired:
                                        raise
                                    except RemoteError:
                                        raise
                                    except Exception:
                                        pass
                                if value is None:
                                    if request.get("refresh"):
                                        # A retained native manager can hold a refresh
                                        # grant rotated by another process. Reload the
                                        # Keychain under this account's shared lock.
                                        await account.close()
                                    await account.start()
                                    result = await account.rpc.call(
                                        "account/read",
                                        {"refreshToken": bool(request.get("refresh"))},
                                    )
                                    if not result.get("account"):
                                        raise SignInRequired()
                                    value = await native_credentials(account, stats)
                                    if request.get("refresh"):
                                        refreshed[label] = time.monotonic()
                                credentials_cache.put(label, stamp, value)
                                permission_failures.pop(label, None)
                            elif op == "usage":
                                value = (
                                    pool.cached(label, not_before=request.get("after"))
                                    if request.get("force") and request.get("after")
                                    else pool.cached(label)
                                    if not request.get("force")
                                    else None
                                )
                                if value is None:
                                    await account.start()
                                    value = await account.rpc.call(
                                        "account/rateLimits/read"
                                    )
                                    pool.save(label, value)
                            elif op == "invalidate":
                                credentials_cache.discard(label)
                                permission_failures.pop(label, None)
                                refreshed.pop(label, None)
                                await account.close()
                                accounts.pop(label, None)
                                value = True
                            else:
                                raise ValueError()
                        response = {"ok": True, "result": value}
            except KeychainAccessRequired:
                if account is not None:
                    permission_failures[label] = time.monotonic()
                response = {"ok": False, "reason": "keychain_access_required"}
            except SignInRequired:
                credentials_cache.discard(request.get("label"))
                if account is not None and not account_auth.required(account.home):
                    account_auth.mark_required(account.home, enrollment=stamp)
                    pool.signal("accounts")
                    pool.signal()
                response = {"ok": False, "reason": "sign_in_required"}
            except RemoteError as error:
                response = {"ok": False}
                if account is not None and account_auth.permanent(error.payload):
                    credentials_cache.discard(label)
                    account_auth.mark_required(account.home, enrollment=stamp)
                    pool.signal("accounts")
                    pool.signal()
                    response["reason"] = "sign_in_required"
            except (Exception, asyncio.CancelledError):
                response = {"ok": False}
            finally:
                with contextlib.suppress(Exception):
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

        server = await asyncio.start_unix_server(
            handle, str(socket), limit=4 * 1024 * 1024
        )
        socket.chmod(0o600)
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), 30)
                except asyncio.TimeoutError:
                    pass
                import settings

                if (
                    time.monotonic() - last_used
                    > settings.load(state)["broker_idle_seconds"]
                    and not pool.detail()
                ):
                    break
        finally:
            server.close()
            await server.wait_closed()
            await asyncio.gather(
                *(a.close() for a in accounts.values()), return_exceptions=True
            )
            pool.close()
            socket.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
