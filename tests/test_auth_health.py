"""Quota remaining does not establish a usable login."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import os
from unittest.mock import patch

import account_auth
import broker
import commands
from pool import Pool
from router import Controller, ControlRejected, RemoteError, SignInRequired
from test_router import FakeAccount, FakeBackend, FakeClient
from test_services import wait_for


class LoginHealthTests(unittest.IsolatedAsyncioTestCase):
    def test_usage_auth_classifier_is_specific_to_native_endpoint_and_status(self):
        message = "failed to fetch codex rate limits: GET https://chatgpt.com/backend-api/wham/usage failed: 401 Unauthorized; body=PRIVATE"
        self.assertTrue(
            account_auth.usage_unauthorized({"code": -32603, "message": message})
        )
        for payload in (
            {"code": -32603, "message": "arbitrary 401 Unauthorized"},
            {"code": -32600, "message": message},
            {
                "code": -32603,
                "message": message.replace("401 Unauthorized", "403 Forbidden"),
            },
            {
                "code": -32603,
                "message": message.replace(
                    "401 Unauthorized", "500 Internal Server Error"
                ),
            },
        ):
            self.assertFalse(account_auth.usage_unauthorized(payload))

    async def test_usage_401_refreshes_once_and_only_marks_login_when_refresh_fails(
        self,
    ):
        unauthorized = {
            "code": -32603,
            "message": "failed to fetch codex rate limits: GET usage failed: 401 Unauthorized; body=PRIVATE",
        }
        for outcome in ("recovered", "revoked", "still_unauthorized", "network"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                home = self.home(state)
                calls = []

                class Account:
                    def __init__(self, label, home, binary, pool):
                        self.home = home
                        self.rpc = self

                    async def start(self):
                        pass

                    async def close(self):
                        pass

                    async def call(self, method, params=None):
                        calls.append((method, params))
                        if method == "account/read":
                            if outcome == "revoked":
                                raise RemoteError(
                                    {
                                        "code": -32603,
                                        "message": next(
                                            iter(
                                                account_auth.PERMANENT_REFRESH_MESSAGES
                                            )
                                        ),
                                    }
                                )
                            if outcome == "network":
                                raise RemoteError(
                                    {"code": -32603, "message": "network unavailable"}
                                )
                            return {"account": {"type": "chatgpt"}}
                        reads = sum(m == "account/rateLimits/read" for m, _ in calls)
                        if reads == 1 or outcome == "still_unauthorized":
                            raise RemoteError(unauthorized)
                        return {"ordinaryUsageAllowed": True}

                with patch("router.Account", Account):
                    server = asyncio.create_task(broker.serve(state, "unused"))
                    try:
                        await wait_for(lambda: broker.paths(state)[1].exists())
                        if outcome == "recovered":
                            result = await broker.exchange(
                                state, {"op": "usage", "label": "one", "force": True}
                            )
                            self.assertTrue(result["ordinaryUsageAllowed"])
                        else:
                            with self.assertRaises(broker.CoordinatorError) as caught:
                                await broker.exchange(
                                    state,
                                    {"op": "usage", "label": "one", "force": True},
                                )
                            self.assertEqual(
                                caught.exception.reason,
                                "sign_in_required" if outcome != "network" else None,
                            )
                        self.assertEqual(sum(m == "account/read" for m, _ in calls), 1)
                        self.assertIn(("account/read", {"refreshToken": True}), calls)
                        self.assertEqual(
                            account_auth.required(home),
                            outcome in {"revoked", "still_unauthorized"},
                        )
                        self.assertLessEqual(
                            sum(m == "account/rateLimits/read" for m, _ in calls), 2
                        )
                    finally:
                        await broker.exchange(state, {"op": "stop"})
                        await asyncio.wait_for(server, 3)

    async def test_selection_does_not_turn_signin_required_into_unavailable_block(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            self.home(state)
            pool = Pool(state)
            account = FakeAccount("one")

            async def usage(force=False):
                account_auth.mark_required(state / "accounts/one")
                raise SignInRequired()

            account.usage = usage
            controller = Controller([account], FakeBackend(), pool=pool)
            try:
                index, deadline = await controller.choose()
                self.assertIsNone(index)
                self.assertEqual(pool.deadline("one"), 0)
                self.assertEqual(
                    controller.waiting_accounts(deadline)[0], "waiting_for_accounts"
                )
            finally:
                pool.close()

    async def test_disabled_account_does_not_set_earliest_retry_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            self.home(state, "off")
            self.home(state, "on")
            pool = Pool(state)
            try:
                pool.set_options("off", enabled=False)
                pool.mark("off", None, reason="unavailable", delay=5)
                on_deadline = pool.mark("on", None, reason="unavailable", delay=100)
                controller = Controller(
                    [FakeAccount("off"), FakeAccount("on")], FakeBackend(), pool=pool
                )
                index, deadline = await controller.choose()
                self.assertIsNone(index)
                self.assertEqual(deadline, on_deadline)
            finally:
                pool.close()

    def test_real_cli_management_errors_are_handled_without_tracebacks(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).parents[1] / "python/router.py"),
                    "switch",
                    "missing",
                    "--account",
                    "one",
                    "--wait",
                ],
                env={**os.environ, "RELAY_STATE": folder},
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("relay: Choose a unique session", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def home(self, state, label="one"):
        home = state / "accounts" / label
        home.mkdir(parents=True, exist_ok=True)
        (home / "enrolled").write_text("{}")
        return home

    def test_login_health_is_scoped_to_the_enrollment_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            home = self.home(Path(folder))
            account_auth.mark_required(home)
            self.assertTrue(account_auth.required(home))
            data = json.loads((home / "login-status.json").read_text())
            self.assertEqual(set(data), {"state", "enrollment", "checked_at"})
            (home / "enrolled").write_text('{"new":true}')
            self.assertFalse(account_auth.required(home))
            account_auth.mark_required(home)
            account_auth.clear(home)
            self.assertFalse(account_auth.required(home))
            account_auth.mark_required(home, enrollment=data["enrollment"])
            self.assertFalse(account_auth.required(home))

    async def broker_case(self, payload, *, expect_signin):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            home = self.home(state)
            calls = []
            failing = True

            class Account:
                def __init__(self, label, home, binary, pool):
                    self.home = home
                    self.rpc = self

                async def start(self):
                    pass

                async def close(self):
                    pass

                async def call(self, method, params=None):
                    calls.append(method)
                    if failing:
                        raise RemoteError(payload)
                    return {"ordinaryUsageAllowed": True}

            with patch("router.Account", Account):
                server = asyncio.create_task(broker.serve(state, "unused"))
                try:
                    await wait_for(lambda: broker.paths(state)[1].exists())
                    with self.assertRaises(broker.CoordinatorError) as raised:
                        await broker.exchange(
                            state, {"op": "usage", "label": "one", "force": True}
                        )
                    self.assertEqual(
                        raised.exception.reason,
                        "sign_in_required" if expect_signin else None,
                    )
                    self.assertEqual(account_auth.required(home), expect_signin)
                    before = len(calls)
                    if expect_signin:
                        for op in ("usage", "credentials"):
                            with self.assertRaises(broker.CoordinatorError):
                                await broker.exchange(state, {"op": op, "label": "one"})
                        self.assertEqual(len(calls), before)
                        (home / "enrolled").write_text('{"reauthenticated":true}')
                    failing = False
                    response = await broker.exchange(
                        state, {"op": "usage", "label": "one", "force": True}
                    )
                    self.assertTrue(response["ordinaryUsageAllowed"])
                finally:
                    await broker.exchange(state, {"op": "stop"})
                    await asyncio.wait_for(server, 3)

    async def test_revoked_login_is_reported_and_not_retried_until_enrollment_changes(
        self,
    ):
        await self.broker_case(
            {
                "code": -32603,
                "message": "Your access token could not be refreshed because your refresh token was revoked. Please log out and sign in again.",
            },
            expect_signin=True,
        )

    async def test_unrelated_server_error_does_not_disable_login(self):
        await self.broker_case(
            {"code": -32603, "message": "PRIVATE_SERVER_DETAIL"}, expect_signin=False
        )

    async def test_client_preserves_safe_signin_reason_without_server_details(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Pool(Path(folder))
            a = broker.ClientAccount("one", Path(folder), "unused", p)

            async def rejected(*args, **kw):
                raise broker.CoordinatorError("sign_in_required")

            with patch("broker.ensure"), patch("broker.exchange", rejected):
                with self.assertRaises(SignInRequired):
                    await a.credentials()
            p.close()

    async def test_routing_skips_revoked_login_and_manual_switch_explains_why(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            home = self.home(state)
            account_auth.mark_required(home)
            p = Pool(state)
            accounts = [FakeAccount("one"), FakeAccount("two")]
            c = Controller(accounts, FakeBackend(), pool=p, report=lambda _: None)
            c.client = FakeClient()
            try:
                index, _ = await c.choose()
                self.assertEqual(index, 1)
                self.assertEqual(accounts[0].checks, 0)
                with self.assertRaises(ControlRejected) as raised:
                    await c.control("switch", {"account": "one"})
                self.assertEqual(raised.exception.reason, "sign_in_required")
                self.assertFalse(c.backend.calls)
                state_name, fields = c.waiting_accounts(2000)
                self.assertEqual(state_name, "waiting_for_accounts")
                self.assertEqual(fields["sign_in_required"], ["one"])
                account_auth.clear(home)
                self.assertEqual(c.waiting_accounts(2000)[0], "waiting_for_reset")
            finally:
                p.close()

    def test_cached_allowance_is_retained_but_not_shown_as_a_usable_login(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            home = self.home(state)
            account_auth.mark_required(home)
            p = Pool(state)
            p.save(
                "one",
                {
                    "ordinaryUsageAllowed": True,
                    "rateLimits": {"primary": {"usedPercent": 0}},
                },
            )
            p.close()
            value = commands.snapshot(state)
            self.assertTrue(value["accounts"][0]["sign_in_required"])
            self.assertEqual(value["accounts"][0]["short_remaining"], "100%")
            self.assertIn("sign-in needed", commands.render(value))

    async def test_successful_manual_switch_resumes_waiting_recovery_on_selected_account(
        self,
    ):
        c = Controller(
            [FakeAccount("one"), FakeAccount("two")],
            FakeBackend(),
            report=lambda _: None,
        )
        c.client = FakeClient()
        c.last_turn["thread-1"] = {"threadId": "thread-1"}
        c.recovery = asyncio.create_task(asyncio.sleep(1000))
        previous = c.recovery
        await asyncio.sleep(0)
        await c.control("switch", {"account": "two"})
        await asyncio.wait_for(c.recovery, 1)
        self.assertTrue(previous.cancelled())
        self.assertEqual(c.account.label, "two")
        self.assertEqual(sum(m == "turn/start" for m, _ in c.backend.calls), 1)
