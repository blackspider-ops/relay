"""Quota recovery at native idle boundaries and account refresh races."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

from pool import failure_kind
from router import Controller, RemoteError, RouterError, RPC
from settings import atomic_json
from test_router import FakeAccount, FakeBackend, FakeClient, completed
from test_services import fake_executable, wait_for
from test_pool import Clock


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_diagnostic_tool_changes_do_not_redraw_unchanged_dashboard(self):
        from watch import fingerprint

        first = {
            "at": 1,
            "sessions": [
                {
                    "id": "task",
                    "state": "working",
                    "account": "one",
                    "seen": 1,
                    "activity": {"items": ["tool-a"]},
                }
            ],
        }
        second = {
            "at": 2,
            "sessions": [
                {
                    "id": "task",
                    "state": "working",
                    "account": "one",
                    "seen": 2,
                    "activity": {"items": ["tool-b"], "disconnected": False},
                }
            ],
        }
        self.assertEqual(fingerprint(first), fingerprint(second))
        second["sessions"][0]["state"] = "waiting_for_reset"
        self.assertNotEqual(fingerprint(first), fingerprint(second))
        second["sessions"][0]["state"] = "working"
        second["sessions"][0]["account"] = "two"
        self.assertNotEqual(fingerprint(first), fingerprint(second))

    def controller(
        self,
        *,
        status="systemError",
        turn_status="failed",
        tool_status="completed",
        backgrounds=True,
    ):
        c = Controller(
            [FakeAccount("one"), FakeAccount("two")],
            FakeBackend(),
            report=lambda _: None,
        )
        c.client = FakeClient()
        c.last_turn["thread-1"] = {"threadId": "thread-1"}
        c.turn_ids["thread-1"] = "turn-1"
        c.active_items.add("orphaned-lifecycle-id")
        c.active_item_threads["orphaned-lifecycle-id"] = "thread-1"
        original = c.backend.call

        async def call(method, params=None, **kw):
            if method == "thread/backgroundTerminals/list":
                return {
                    "data": [
                        {
                            "itemId": "orphaned-lifecycle-id",
                            "processId": "background-job",
                        }
                    ]
                    if backgrounds
                    else []
                }
            if method == "thread/turns/list":
                return {
                    "data": [
                        {
                            "id": "turn-1",
                            "status": turn_status,
                            "itemsView": "full",
                            "items": [
                                {
                                    "id": "persisted-action",
                                    "type": "commandExecution",
                                    "status": tool_status,
                                }
                            ],
                        }
                    ]
                }
            if method == "thread/read":
                return {
                    "thread": {"status": {"type": status}, "canAcceptDirectInput": True}
                }
            return await original(method, params, **kw)

        c.backend.call = call
        return c

    async def test_known_background_terminal_unblocks_quota_without_replaying_request(
        self,
    ):
        c = self.controller()
        await c.event(completed())
        await asyncio.wait_for(c.recovery, 1)
        self.assertFalse(c.active_items)
        self.assertEqual(c.account.label, "two")
        starts = [p for m, p in c.backend.calls if m == "turn/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["threadId"], "thread-1")

    async def test_active_or_incomplete_native_work_still_blocks(self):
        for options in (
            {"status": "active"},
            {"status": "notLoaded"},
            {"turn_status": "inProgress"},
            {"tool_status": "inProgress", "backgrounds": False},
            {"tool_status": None, "backgrounds": False},
            {"backgrounds": False},
        ):
            with self.subTest(options=options):
                c = self.controller(**options)
                await c.reconcile_active_items()
                self.assertTrue(c.active_items)

    async def test_manual_switch_keeps_known_background_job_without_new_turn(self):
        c = self.controller()
        await c.control("switch", {"account": "two"})
        self.assertEqual(c.account.label, "two")
        self.assertFalse(c.active_items)
        self.assertFalse(any(m == "turn/start" for m, _ in c.backend.calls))

    async def test_pending_approval_and_different_current_turn_still_block(self):
        for blocker in ("approval", "different-turn"):
            c = self.controller()
            if blocker == "approval":
                c.server_requests.add(99)
            else:
                c.turn_ids["thread-1"] = "newer-turn"
            await c.reconcile_active_items()
            self.assertTrue(c.active_items)

    async def test_new_user_work_during_status_read_invalidates_boundary(self):
        c = self.controller()
        original = c.backend.call

        async def call(method, params=None, **kw):
            result = await original(method, params, **kw)
            if method == "thread/read":
                c.user_generation += 1
            return result

        c.backend.call = call
        await c.reconcile_active_items()
        self.assertTrue(c.active_items)

    async def test_refresh_callback_during_login_uses_new_account(self):
        c = self.controller()
        original = c.backend.call

        async def call(method, params=None, **kw):
            if method == "account/login/start":
                await c.event(
                    {
                        "id": "refresh",
                        "method": "account/chatgptAuthTokens/refresh",
                        "params": {"previousAccountId": "two"},
                    }
                )
            return await original(method, params, **kw)

        c.backend.call = call
        await asyncio.wait_for(c.login(1), 1)
        self.assertEqual(c.backend.sent[-1]["result"]["chatgptAccountId"], "two")
        self.assertFalse(c.client.messages)

    async def test_failed_refresh_is_private_and_classified_for_auth_retry(self):
        c = self.controller()

        async def failed(refresh=False):
            raise RouterError("PRIVATE_EXCEPTION_DETAIL")

        c.accounts[0].credentials = failed
        await c.event(
            {"id": 3, "method": "account/chatgptAuthTokens/refresh", "params": {}}
        )
        self.assertEqual(c.backend.sent[-1]["error"]["code"], -32000)
        self.assertNotIn("PRIVATE_EXCEPTION_DETAIL", json.dumps(c.backend.sent))
        self.assertFalse(c.client.messages)
        self.assertEqual(
            failure_kind("other", "Auth refresh request failed: code=-32000"), "auth"
        )
        self.assertIsNone(
            failure_kind("badRequest", "Auth refresh request failed: code=-32000")
        )
        self.assertIsNone(
            failure_kind("other", "unrelated request failed: code=-32000")
        )

    async def test_refresh_from_replaced_backend_cannot_reach_new_connection(self):
        c = self.controller()
        old = c.backend

        async def rotating(refresh=False):
            c.backend = FakeBackend()
            return {"accessToken": "FAKE", "chatgptAccountId": "one"}

        c.account.credentials = rotating
        await c.event(
            {"id": 12, "method": "account/chatgptAuthTokens/refresh", "params": {}},
            origin=old,
        )
        self.assertFalse(c.backend.sent)
        self.assertFalse(old.sent)
        await c.event(completed(), origin=old)
        self.assertIsNone(c.recovery)

    async def test_auth_rejection_before_acceptance_retries_original_input_once(self):
        c = self.controller()
        c.active_items.clear()
        c.active_item_threads.clear()
        clock = Clock()
        c.clock, c.sleep = clock, clock.sleep
        c.retry_interval = 300
        original = c.backend.call
        requests = []

        async def call(method, params=None, **kw):
            if method == "turn/start":
                requests.append(dict(params))
                if len(requests) == 1:
                    raise RemoteError(
                        {
                            "code": -32603,
                            "message": "auth refresh request failed: code=-32000",
                        }
                    )
                return {"turn": {"id": "accepted-turn", "status": "inProgress"}}
            return await original(method, params, **kw)

        c.backend.call = call
        await c.forward(
            {
                "id": 1,
                "method": "turn/start",
                "params": {
                    "threadId": "thread-1",
                    "input": [
                        {
                            "type": "text",
                            "text": "UNIQUE_NEW_REQUEST",
                            "text_elements": [],
                        }
                    ],
                },
            },
            {},
        )
        await asyncio.wait_for(c.recovery, 1)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["input"], requests[1]["input"])
        self.assertEqual(
            requests[0]["clientUserMessageId"], requests[1]["clientUserMessageId"]
        )
        self.assertEqual(c.account.label, "one")
        self.assertFalse(c.rejected_turns)
        self.assertEqual(c.turn_ids["thread-1"], "accepted-turn")

    async def test_changed_history_prevents_replay_of_rejected_input(self):
        c = self.controller()
        c.active_items.clear()
        c.remember_rejected(
            "thread-1", {"threadId": "thread-1", "input": []}, "older-turn"
        )
        await c.continue_thread("thread-1", 0)
        self.assertIn("thread-1", c.paused_uncertain)
        self.assertFalse(any(m == "turn/start" for m, _ in c.backend.calls))

    async def test_worker_restart_does_not_guess_rejected_prompt(self):
        c = self.controller()
        await c.restore_recovery(
            "thread-1",
            {"rejected": True, "accepted": False, "state": "retry_scheduled"},
        )
        self.assertIn("thread-1", c.paused_uncertain)
        self.assertFalse(any(m == "turn/start" for m, _ in c.backend.calls))

    async def test_watchdog_detects_eof_while_native_process_is_still_alive(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os,time; os.close(1); time.sleep(30)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        rpc = RPC(proc)
        c = Controller([FakeAccount("one")], rpc, report=lambda _: None)
        c.backend_factory = lambda: None
        restarted = []

        async def restart():
            restarted.append(True)
            c.stopping.set()

        c.restart_backend = restart
        try:
            await asyncio.wait_for(asyncio.shield(rpc.reader), 3)
            self.assertIsNone(proc.returncode)
            await asyncio.wait_for(c.maintain(), 3)
            await c.restart_task
            self.assertTrue(restarted)
        finally:
            await rpc.close()

    async def test_broker_refresh_reloads_persisted_grant_under_shared_lock(self):
        import base64
        import time
        import broker

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            home = state / "accounts" / "one"
            home.mkdir(parents=True)
            (home / "enrolled").write_text("{}")
            events = []

            class Account:
                def __init__(self, label, home, binary, pool):
                    self.home = home
                    self.rpc = FakeBackend()

                async def start(self):
                    events.append("start")

                async def close(self):
                    events.append("close")

            part = (
                base64.urlsafe_b64encode(
                    json.dumps({"exp": time.time() + 3600}).encode()
                )
                .decode()
                .rstrip("=")
            )
            credentials = {
                "accessToken": "fake." + part + ".fake",
                "chatgptAccountId": "one",
            }
            with (
                patch("router.Account", Account),
                patch("router.read_keychain", return_value=credentials),
            ):
                server = asyncio.create_task(broker.serve(state, "unused"))
                try:
                    await wait_for(lambda: broker.paths(state)[1].exists())
                    await broker.exchange(state, {"op": "usage", "label": "one"})
                    value = await broker.exchange(
                        state, {"op": "credentials", "label": "one", "refresh": True}
                    )
                    self.assertEqual(value, credentials)
                    self.assertEqual(events[:3], ["start", "close", "start"])
                finally:
                    await broker.exchange(state, {"op": "stop"})
                    await asyncio.wait_for(server, 3)

    async def test_background_activity_over_real_jsonl_switches_without_restart(self):
        class ResetPromptClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.blocked = False
                self.saw_reset_prompt = False

            async def send(self, raw):
                message = json.loads(raw)
                params = message.get("params", {})
                error = params.get("error") or params.get("turn", {}).get("error") or {}
                if (
                    message.get("method") in {"error", "turn/completed"}
                    and error.get("codexErrorInfo") == "usageLimitExceeded"
                ):
                    self.blocked = True
                    self.saw_reset_prompt = True
                if message.get("method") == "account/updated":
                    self.blocked = False
                await super().send(raw)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            atomic_json(
                root / "fault.json", {"quota_turns": 1, "background_activity_turns": 1}
            )
            rpc = await RPC.start(fake_executable(root), root)
            c = Controller(
                [FakeAccount("one"), FakeAccount("two")], rpc, report=lambda _: None
            )
            c.client = ResetPromptClient()
            rpc.handler = c.event
            try:
                pid = rpc.proc.pid
                await rpc.initialize()
                await c.forward(
                    {"id": "thread", "method": "thread/start", "params": {}}, {}
                )
                thread = next(iter(c.last_turn))
                await c.forward(
                    {
                        "id": "turn",
                        "method": "turn/start",
                        "params": {
                            "threadId": thread,
                            "input": [
                                {"type": "text", "text": "work", "text_elements": []}
                            ],
                        },
                    },
                    {},
                )

                def recovered():
                    turns = json.loads((root / "fake-history.json").read_text())[
                        "threads"
                    ][thread]["turns"]
                    return len(turns) == 2 and turns[-1]["status"] == "completed"

                await wait_for(recovered)
                self.assertEqual(c.account.label, "two")
                self.assertFalse(c.active_items)
                self.assertEqual(rpc.proc.pid, pid)
                history = json.loads((root / "fake-history.json").read_text())
                self.assertTrue(history["backgrounds"][thread])
                self.assertTrue(c.client.saw_reset_prompt)
                self.assertFalse(c.client.blocked)
                self.assertFalse(c.server_requests)
                self.assertFalse(
                    any(
                        c["method"] == "account/rateLimitResetCredit/consume"
                        for c in history["calls"]
                    )
                )
            finally:
                if c.recovery:
                    c.recovery.cancel()
                    await asyncio.gather(c.recovery, return_exceptions=True)
                await rpc.close()
