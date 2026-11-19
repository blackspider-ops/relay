"""Sign out only the chosen account, after clearing its shared credentials."""

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import broker
import commands
import completion
from pool import Pool
from router import RouterError, labels, parser, selected_binary
from settings import atomic_json
from test_services import wait_for


class SignOutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.home = self.state / "accounts" / "personal"
        self.home.mkdir(parents=True)
        atomic_json(self.home / "enrolled", {"identity": "old"})
        atomic_json(self.home / "identity.json", {"email": "person@example.invalid"})
        atomic_json(self.home / "login-status.json", {"state": "sign_in_required"})
        self.pool = Pool(self.state)
        self.addCleanup(self.pool.close)
        self.pool.set_options("personal", enabled=True, name="Personal")
        self.pool.save("personal", {"ordinaryUsageAllowed": True})
        self.binary = str(self.state / "fake-codex")

    def process(self, code=0):
        process = AsyncMock()
        process.wait.return_value = code
        process.returncode = code
        return process

    async def manage(self, arguments, native, exchange=None):
        with (
            patch("asyncio.create_subprocess_exec", native),
            patch("broker.exchange", exchange or AsyncMock(return_value=True)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            result = await commands.manage(
                parser().parse_args(arguments), self.state, self.binary
            )
        return result, output.getvalue()

    async def test_signout_by_email_clears_cache_and_native_login_and_preserves_work(
        self,
    ):
        other = self.state / "accounts" / "work"
        other.mkdir(parents=True)
        (other / "enrolled").write_text("{}")
        saved = self.state / "saved-conversation.json"
        saved.write_text('"keep me"')
        order = []

        async def invalidate(state, request, timeout):
            self.assertEqual(request, {"op": "invalidate", "label": "personal"})
            self.assertTrue(self.pool.options("personal")["maintenance"])
            order.append("invalidate")
            return True

        async def launch(*args, **kwargs):
            order.append("logout")
            self.assertEqual(args[:2], (self.binary, "logout"))
            self.assertIn('cli_auth_credentials_store="keyring"', args)
            self.assertIn("features.secret_auth_storage=false", args)
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.home))
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            return self.process()

        native = AsyncMock(side_effect=launch)
        result, output = await self.manage(
            ["account", "signout", "person@example.invalid"], native, invalidate
        )
        self.assertEqual(result, 0)
        self.assertEqual(order, ["invalidate", "logout"])
        self.assertIn("Signed out", output)
        self.assertEqual(labels(self.state), ["work"])
        self.assertFalse((self.home / "login-status.json").exists())
        self.assertTrue((self.home / "enrolled.removed").exists())
        self.assertIsNone(self.pool.cached("personal"))
        self.assertFalse(self.pool.options("personal")["enabled"])
        self.assertFalse(self.pool.options("personal")["maintenance"])
        self.assertEqual(saved.read_text(), '"keep me"')
        self.assertEqual(
            json.loads((self.home / "identity.json").read_text())["email"],
            "person@example.invalid",
        )

    async def test_aliases_can_sign_out_the_only_account(self):
        for arguments in (
            ["signout", "Personal"],
            ["logout", "personal"],
            ["account", "logout", "personal"],
            ["account", "sign-out", "personal"],
            ["account", "remove", "personal", "--revoke"],
        ):
            with self.subTest(arguments=arguments):
                atomic_json(self.home / "enrolled", {})
                native = AsyncMock(return_value=self.process())
                await self.manage(arguments, native)
                native.assert_awaited_once()
                self.assertEqual(labels(self.state), [])

    async def test_live_sessions_and_reservations_prevent_signout(self):
        for blocker in ("session", "reservation"):
            with self.subTest(blocker=blocker):
                owner = Pool(self.state)
                owner.session = "another-worker"
                try:
                    if blocker == "session":
                        owner.assign("personal")
                    else:
                        self.assertTrue(owner.reserve("personal"))
                    native = AsyncMock()
                    exchange = AsyncMock()
                    with self.assertRaisesRegex(RouterError, "Switch those sessions"):
                        await self.manage(
                            ["account", "signout", "personal"], native, exchange
                        )
                    native.assert_not_called()
                    exchange.assert_not_called()
                    self.assertTrue((self.home / "enrolled").exists())
                finally:
                    owner.close()

    async def test_native_or_coordinator_failure_retains_account(self):
        for phase in ("native", "coordinator", "timeout"):
            with self.subTest(phase=phase):
                native = AsyncMock(
                    return_value=self.process(1 if phase == "native" else 0)
                )
                exchange = AsyncMock(return_value=True)
                if phase == "coordinator":
                    exchange.side_effect = broker.CoordinatorError()
                if phase == "timeout":
                    native.return_value.wait.side_effect = asyncio.TimeoutError()
                    native.return_value.returncode = None
                    native.return_value.kill = lambda: setattr(
                        native.return_value, "returncode", -9
                    )
                    native.return_value.wait.side_effect = [asyncio.TimeoutError(), -9]
                with self.assertRaises(RouterError):
                    await self.manage(["signout", "personal"], native, exchange)
                self.assertTrue((self.home / "enrolled").exists())
                self.assertTrue(self.pool.options("personal")["enabled"])
                self.assertFalse(self.pool.options("personal")["maintenance"])
                if phase == "coordinator":
                    native.assert_not_called()

    async def test_no_running_coordinator_is_not_required_for_signout(self):
        native = AsyncMock(return_value=self.process())
        await self.manage(
            ["signout", "personal"], native, AsyncMock(side_effect=FileNotFoundError())
        )
        self.assertEqual(labels(self.state), [])

    async def test_signout_upgrades_existing_coordinator_before_invalidation(self):
        atomic_json(broker.paths(self.state)[1], {"token": "fake"})
        native = AsyncMock(return_value=self.process())
        with patch("broker.ensure", new_callable=AsyncMock) as ensure:
            await self.manage(["signout", "personal"], native)
        ensure.assert_awaited_once_with(self.state, self.binary, current=True)

    async def test_native_launch_failure_is_safe_and_keeps_entry(self):
        native = AsyncMock(side_effect=FileNotFoundError("PRIVATE_PATH"))
        with self.assertRaisesRegex(RouterError, "Codex could not start") as caught:
            await self.manage(["signout", "personal"], native)
        self.assertNotIn("PRIVATE_PATH", str(caught.exception))
        self.assertTrue((self.home / "enrolled").exists())
        self.assertFalse(self.pool.options("personal")["maintenance"])

    async def test_remove_without_revoke_still_retains_native_login(self):
        native = AsyncMock()
        _, output = await self.manage(["account", "remove", "personal"], native)
        native.assert_not_called()
        self.assertIn("Keychain login retained", output)

    def test_completion_offers_signout_and_account_emails_without_credentials(self):
        self.assertIn("signout", completion.candidates([""], self.state))
        self.assertIn("signout", completion.candidates(["account", "sign"], self.state))
        self.assertIn(
            "person@example.invalid", completion.candidates(["signout", ""], self.state)
        )

    def test_real_cli_menu_and_shortcut_routes_use_isolated_native_logout(self):
        native = self.state / "codex"
        native.write_text(
            "#!" + sys.executable + "\n"
            "import json,os,sys\nfrom pathlib import Path\n"
            "home=Path(os.environ['CODEX_HOME'])\n"
            "assert sys.argv[1]=='logout'\n"
            "assert 'cli_auth_credentials_store=\"keyring\"' in sys.argv\n"
            "(home/'fake-logout.json').write_text(json.dumps(sys.argv[1:]))\n"
        )
        native.chmod(0o700)
        for arguments in (["signout", "personal"], ["account", "signout", "personal"]):
            with self.subTest(arguments=arguments):
                atomic_json(self.home / "enrolled", {})
                with patch("router.codex_path", return_value=str(native)):
                    self.assertEqual(
                        selected_binary(parser().parse_args(arguments), self.state),
                        str(native),
                    )
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).parents[1] / "python/router.py"),
                        *arguments,
                    ],
                    env={
                        **os.environ,
                        "PATH": str(self.state) + os.pathsep + os.environ["PATH"],
                        "RELAY_STATE": str(self.state),
                    },
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Signed out", result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                self.assertTrue((self.home / "fake-logout.json").exists())
                self.assertEqual(labels(self.state), [])

    async def test_queued_helper_read_rechecks_maintenance_before_recreating_cache(
        self,
    ):
        self.pool.invalidate("personal")
        entered = asyncio.Event()
        release = asyncio.Event()
        reads = []

        class Account:
            def __init__(self, label, home, binary, pool):
                self.home = home
                self.rpc = self

            async def start(self):
                pass

            async def close(self):
                pass

            async def call(self, method, params=None):
                reads.append(method)
                entered.set()
                await release.wait()
                return {"ordinaryUsageAllowed": True}

        server = None
        pending = []
        with patch("router.Account", Account):
            try:
                server = asyncio.create_task(broker.serve(self.state, self.binary))
                await wait_for(lambda: broker.paths(self.state)[1].exists())
                first = asyncio.create_task(
                    broker.exchange(self.state, {"op": "usage", "label": "personal"})
                )
                pending.append(first)
                await asyncio.wait_for(entered.wait(), 3)
                queued = asyncio.create_task(
                    broker.exchange(self.state, {"op": "usage", "label": "personal"})
                )
                pending.append(queued)
                await asyncio.sleep(0.05)
                self.assertTrue(self.pool.begin_maintenance("personal"))
                invalidate = asyncio.create_task(
                    broker.exchange(
                        self.state, {"op": "invalidate", "label": "personal"}
                    )
                )
                pending.append(invalidate)
                release.set()
                await first
                with self.assertRaises(broker.CoordinatorError):
                    await queued
                self.assertTrue(await invalidate)
                self.assertEqual(reads, ["account/rateLimits/read"])
                (self.home / "enrolled").unlink()
                with self.assertRaises(broker.CoordinatorError):
                    await broker.exchange(
                        self.state, {"op": "credentials", "label": "personal"}
                    )
            finally:
                release.set()
                await asyncio.gather(*pending, return_exceptions=True)
                with contextlib.suppress(Exception):
                    await broker.exchange(self.state, {"op": "stop"})
                if server:
                    server.cancel()
                    await asyncio.gather(server, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
