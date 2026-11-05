"""A lost item completion must not strand recovery or clear unknown work."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from router import Controller, RPC, RouterError
from test_router import FakeAccount, FakeBackend, FakeClient, completed


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_completion_over_real_jsonl_recovers_without_backend_restart(
        self,
    ):
        from test_services import fake_executable, wait_for
        from settings import atomic_json

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            atomic_json(
                root / "fault.json", {"quota_turns": 1, "lost_completion_turns": 1}
            )
            rpc = await RPC.start(fake_executable(root), root)
            c = Controller(
                [FakeAccount("one"), FakeAccount("two")], rpc, report=lambda _: None
            )
            c.client = FakeClient()
            rpc.handler = c.event
            try:
                initial_pid = rpc.proc.pid
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
                                {
                                    "type": "text",
                                    "text": "fake work",
                                    "text_elements": [],
                                }
                            ],
                        },
                    },
                    {},
                )
                history = root / "fake-history.json"

                def recovered():
                    turns = json.loads(history.read_text())["threads"][thread]["turns"]
                    return len(turns) == 2 and turns[-1]["status"] == "completed"

                await wait_for(recovered)
                self.assertEqual(c.account.label, "two")
                self.assertFalse(c.active_items)
                self.assertEqual(rpc.proc.pid, initial_pid)
                self.assertIsNone(rpc.proc.returncode)
            finally:
                if c.recovery:
                    await asyncio.gather(c.recovery, return_exceptions=True)
                await rpc.close()

    def controller(self):
        c = Controller(
            [FakeAccount("one"), FakeAccount("two")],
            FakeBackend(),
            report=lambda _: None,
        )
        c.client = FakeClient()
        c.last_turn["thread-1"] = {"threadId": "thread-1"}
        c.preferences["watchdog_interval"] = 0.01
        return c

    async def started(self, c, ident="tool", thread="thread-1"):
        await c.event(
            {
                "method": "item/started",
                "params": {
                    "threadId": thread,
                    "item": {
                        "id": ident,
                        "type": "commandExecution",
                        "status": "inProgress",
                    },
                },
            }
        )

    def history(self, c, status="completed", view="full", ident="tool"):
        original = c.backend.call

        async def call(method, params=None, **kw):
            if method == "thread/turns/list" and params.get("itemsView") == "full":
                return {
                    "data": [
                        {
                            "itemsView": view,
                            "items": [
                                {
                                    "id": ident,
                                    "type": "commandExecution",
                                    "status": status,
                                }
                            ],
                        }
                    ]
                }
            return await original(method, params, **kw)

        c.backend.call = call

    async def test_quota_after_lost_completion_switches_and_continues_same_thread(self):
        c = self.controller()
        await self.started(c)
        self.history(c)
        await c.event(completed())
        await asyncio.wait_for(c.recovery, 1)
        self.assertFalse(c.active_items)
        self.assertEqual(c.account.label, "two")
        turns = [p for m, p in c.backend.calls if m == "turn/start"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["threadId"], "thread-1")

    async def test_running_missing_and_summary_items_remain_blocking(self):
        for status, view, ident in (
            ("inProgress", "full", "tool"),
            ("completed", "summary", "tool"),
            ("completed", "full", "different"),
            (None, "full", "tool"),
        ):
            c = self.controller()
            await self.started(c)
            self.history(c, status, view, ident)
            await c.reconcile_active_items()
            self.assertEqual(c.active_items, {"tool"})

    async def test_finished_tool_does_not_clear_pending_approval_or_child_thread(self):
        c = self.controller()
        await self.started(c)
        c.server_requests.add(7)
        c.active_threads.add("child")
        self.history(c)
        await c.reconcile_active_items()
        self.assertFalse(c.active_items)
        self.assertEqual(c.server_requests, {7})
        self.assertEqual(c.active_threads, {"child"})
        self.assertFalse(any(m == "account/login/start" for m, _ in c.backend.calls))

    async def test_new_start_during_history_read_invalidates_the_read(self):
        c = self.controller()
        await self.started(c)

        async def call(method, params=None, **kw):
            await self.started(c, "new")
            return {
                "data": [
                    {
                        "itemsView": "full",
                        "items": [
                            {
                                "id": "tool",
                                "type": "commandExecution",
                                "status": "completed",
                            }
                        ],
                    }
                ]
            }

        c.backend.call = call
        await c.reconcile_active_items()
        self.assertEqual(c.active_items, {"tool", "new"})

    async def test_full_history_pages_until_matching_item_and_stops_on_repeated_cursor(
        self,
    ):
        c = self.controller()
        await self.started(c)
        calls = []

        async def call(method, params=None, **kw):
            calls.append(params)
            if not params.get("cursor"):
                return {"data": [], "nextCursor": "older"}
            return {
                "data": [
                    {
                        "itemsView": "full",
                        "items": [
                            {
                                "id": "tool",
                                "type": "commandExecution",
                                "status": "failed",
                            }
                        ],
                    }
                ]
            }

        c.backend.call = call
        await c.reconcile_active_items()
        self.assertFalse(c.active_items)
        self.assertEqual(len(calls), 2)
        await self.started(c)
        calls.clear()

        async def repeated(method, params=None, **kw):
            calls.append(params)
            return {"data": [], "nextCursor": "same"}

        c.backend.call = repeated
        await c.reconcile_active_items()
        self.assertEqual(len(calls), 2)
        self.assertEqual(c.active_items, {"tool"})

    async def test_failed_read_and_unowned_item_never_clear_activity(self):
        c = self.controller()
        await self.started(c)
        c.active_items.add("unknown-owner")

        async def failed(*args, **kwargs):
            raise RouterError("unavailable")

        c.backend.call = failed
        await c.reconcile_active_items()
        self.assertEqual(c.active_items, {"tool", "unknown-owner"})

    async def test_completion_and_terminal_start_do_not_leave_stale_ownership(self):
        c = self.controller()
        await self.started(c)
        await c.event(
            {
                "method": "item/completed",
                "params": {"threadId": "thread-1", "item": {"id": "tool"}},
            }
        )
        self.assertFalse(c.active_item_threads)
        await c.event(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "id": "already-done",
                        "type": "commandExecution",
                        "status": "completed",
                    },
                },
            }
        )
        self.assertFalse(c.active_items)


if __name__ == "__main__":
    unittest.main()
