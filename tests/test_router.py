"""Offline tests: no real accounts, Keychain access, or model calls."""

import asyncio
import json
from pathlib import Path
import sys
import unittest

from router import (
    Controller,
    RPC,
    RemoteError,
    RouterError,
    account_home,
    limited,
    transport_guard,
)


class FakeAccount:
    def __init__(self, label, blocked=False, error=False):
        self.label = label
        self.blocked = blocked
        self.error = error
        self.checks = 0

    async def usage(self, force=False):
        self.checks += 1
        if self.error:
            raise RouterError("unavailable")
        return {"ordinaryUsageAllowed": not self.blocked}

    async def credentials(self, refresh=False):
        return {"accessToken": "FAKE_NOT_A_CREDENTIAL", "chatgptAccountId": self.label}


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.sent = []

    async def call(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == "account/read":
            return {"account": {"type": "chatgpt"}}
        return {"ok": True}

    async def send(self, message):
        self.sent.append(message)


class FakeClient:
    def __init__(self):
        self.messages = []

    async def send(self, raw):
        self.messages.append(json.loads(raw))


def completed(
    code="usageLimitExceeded", status="failed", thread="thread-1", turn="turn-1"
):
    return {
        "method": "turn/completed",
        "params": {
            "threadId": thread,
            "turn": {"id": turn, "status": status, "error": {"codexErrorInfo": code}},
        },
    }


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend()
        self.accounts = [FakeAccount("one"), FakeAccount("two"), FakeAccount("three")]
        self.reports = []
        self.c = Controller(
            self.accounts, self.backend, self.reports.append, interval=0.02
        )
        self.c.client = FakeClient()
        self.c.last_turn["thread-1"] = {
            "threadId": "thread-1",
            "model": "chosen-model",
            "input": [{"text": "UNIQUE_ORIGINAL_USER_REQUEST"}],
        }

    async def asyncTearDown(self):
        if self.c.recovery:
            self.c.recovery.cancel()
            await asyncio.gather(self.c.recovery, return_exceptions=True)

    async def test_quota_switch_and_continue_exact_thread_without_replay(self):
        await self.c.event(completed())
        await asyncio.wait_for(self.c.recovery, 1)
        self.assertEqual(self.c.index, 1)
        self.assertEqual(
            [name for name, _ in self.backend.calls],
            ["account/login/start", "account/read", "turn/start"],
        )
        params = self.backend.calls[-1][1]
        self.assertEqual(params["threadId"], "thread-1")
        self.assertNotIn("UNIQUE_ORIGINAL_USER_REQUEST", params["input"][0]["text"])
        self.assertNotIn("approvalPolicy", params)  # Retain existing thread policy.
        self.assertNotIn(
            "model", params
        )  # Retain the sticky model; no silent downgrade.

    async def test_duplicates_do_not_create_two_turns(self):
        message = completed()
        await self.c.event(message)
        await self.c.event(message)
        await self.c.recovery
        await self.c.event(message)
        self.assertEqual(sum(name == "turn/start" for name, _ in self.backend.calls), 1)

    async def test_policy_errors_and_success_never_rotate(self):
        for code in [
            "cyberPolicy",
            "misalignmentPolicyViolation",
            "contextWindowExceeded",
            "badRequest",
        ]:
            await self.c.event(completed(code=code, turn=code))
        await self.c.event(completed(status="completed"))
        await self.c.event(completed(status="interrupted"))
        self.assertIsNone(self.c.recovery)
        self.assertEqual(self.backend.calls, [])

    async def test_untracked_thread_never_resubmitted(self):
        await self.c.event(completed(thread="subagent"))
        self.assertIsNone(self.c.recovery)

    async def test_skips_blocked_and_failed_accounts(self):
        self.accounts[1].blocked = True
        await self.c.event(completed())
        await self.c.recovery
        self.assertEqual(self.c.index, 2)

    async def test_pending_approval_prevents_switch(self):
        self.c.server_requests.add(991)
        await self.c.event(completed())
        await asyncio.sleep(0.03)
        self.assertEqual(self.backend.calls, [])
        await self.c.forward({"id": 991, "result": {"decision": "accept"}}, {})
        self.assertEqual(self.backend.sent[-1]["id"], 991)
        self.c.recovery.cancel()

    async def test_running_subtask_prevents_switch(self):
        self.c.active_threads.add("still-running")
        await self.c.event(completed())
        await asyncio.sleep(0.03)
        self.assertEqual(self.backend.calls, [])

    async def test_resolved_server_request_does_not_leave_stale_approval_wait(self):
        self.c.server_requests.add(991)
        await self.c.event(
            {
                "method": "serverRequest/resolved",
                "params": {"requestId": 991, "threadId": "thread-1"},
            }
        )
        self.assertFalse(self.c.server_requests)
        await self.c.event(completed())
        await self.c.recovery
        self.assertEqual(self.c.index, 1)

    async def test_user_interrupt_cancels_wait(self):
        self.accounts[1].blocked = self.accounts[2].blocked = True
        await self.c.event(completed())
        await asyncio.sleep(0.005)
        await self.c.forward(
            {"id": 7, "method": "turn/interrupt", "params": {"threadId": "thread-1"}},
            {},
        )
        await asyncio.sleep(0.005)
        self.assertTrue(self.c.recovery.cancelled())
        self.assertFalse(
            any(name == "account/login/start" for name, _ in self.backend.calls)
        )

    async def test_no_busy_loop_when_all_accounts_exhausted(self):
        for account in self.accounts:
            account.blocked = True
        await self.c.event(completed())
        await asyncio.sleep(0.055)
        self.assertLessEqual(sum(account.checks for account in self.accounts), 9)
        self.assertEqual(self.backend.calls, [])

    async def test_refresh_never_returns_different_account(self):
        await self.c.event(
            {
                "id": 88,
                "method": "account/chatgptAuthTokens/refresh",
                "params": {"previousAccountId": "different"},
            }
        )
        self.assertIn("error", self.backend.sent[-1])
        self.assertFalse(self.c.client.messages)

    async def test_refresh_credentials_not_forwarded_to_frontend(self):
        await self.c.event(
            {
                "id": 88,
                "method": "account/chatgptAuthTokens/refresh",
                "params": {"previousAccountId": "one"},
            }
        )
        self.assertIn("result", self.backend.sent[-1])
        self.assertFalse(self.c.client.messages)

    async def test_pool_auth_cannot_be_replaced_by_frontend_logout(self):
        await self.c.forward({"id": 1, "method": "account/logout"}, {})
        self.assertEqual(self.backend.calls, [])
        self.assertIn("error", self.c.client.messages[-1])


class PolicyTests(unittest.TestCase):
    def test_percentage_alone_is_not_a_block(self):
        self.assertFalse(limited({"rateLimits": {"primary": {"usedPercent": 100}}}))
        self.assertTrue(limited({"ordinaryUsageAllowed": False}))
        self.assertTrue(
            limited({"rateLimits": {"rateLimitReachedType": "rate_limit_reached"}})
        )

    def test_other_model_bucket_is_not_a_global_block(self):
        value = {
            "rateLimitsByLimitId": {
                "other": {
                    "normalModelSlug": "other-model",
                    "rateLimitReachedType": "rate_limit_reached",
                }
            }
        }
        self.assertFalse(limited(value, "chosen-model"))
        self.assertTrue(limited(value, "other-model"))

    def test_label_cannot_escape_account_root(self):
        for label in ["../bad", "/tmp/bad", "a/b", "", ".", "a\n"]:
            with self.assertRaises(RouterError):
                account_home(Path("/private/test"), label)


class RPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiplexing_errors_and_disconnect(self):
        program = """
import json,sys
for raw in sys.stdin:
 d=json.loads(raw)
 if d['method']=='quit': break
 if d['method']=='bad':
  reply={'id':d['id'],'error':{'code':-32602,'message':'specific client error'}}
 else: reply={'id':d['id'],'result':{'echo':d['params']}}
 print(json.dumps(reply),flush=True)
"""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            program,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        rpc = RPC(proc)
        try:
            a, b = await asyncio.gather(
                rpc.call("echo", {"a": 1}), rpc.call("echo", {"b": 2})
            )
            self.assertEqual(a, {"echo": {"a": 1}})
            self.assertEqual(b, {"echo": {"b": 2}})
            with self.assertRaises(RemoteError) as ctx:
                await rpc.call("bad", {})
            self.assertEqual(ctx.exception.payload["code"], -32602)
            with self.assertRaises(RouterError):
                await rpc.call("quit", {}, timeout=1)
        finally:
            await rpc.close()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_transport_requires_bearer_and_rejects_browser_origins(self):
        from websockets.asyncio.server import serve
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        async def echo(ws):
            async for message in ws:
                await ws.send(message)

        async with serve(
            echo, "127.0.0.1", 0, process_request=transport_guard("test-token")
        ) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            for headers in [
                {},
                {"Authorization": "Bearer wrong"},
                {
                    "Authorization": "Bearer test-token",
                    "Origin": "https://example.invalid",
                },
            ]:
                with self.assertRaises(InvalidStatus):
                    async with connect(url, additional_headers=headers, proxy=None):
                        pass
            async with connect(
                url,
                additional_headers={"Authorization": "Bearer test-token"},
                proxy=None,
            ) as ws:
                await ws.send("test")
                self.assertEqual(await ws.recv(), "test")


if __name__ == "__main__":
    unittest.main()
