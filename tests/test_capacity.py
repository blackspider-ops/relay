"""Capacity recovery and audit regressions, without real accounts or requests."""

import asyncio
import contextlib
import io
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pool import CAPACITY_MESSAGE, Pool, failure_kind
from router import Controller
from test_router import FakeAccount, FakeBackend, FakeClient, completed
from test_pool import Clock
from test_services import wait_for


class CapacityClassifierTests(unittest.TestCase):
    def test_structured_and_exact_legacy_capacity_errors(self):
        self.assertEqual(failure_kind("policyCapacityUnavailable"), "capacity")
        for code in (
            None,
            "other",
            "policyCapacityUnavailable",
            "serverOverloaded",
            "flexUnavailable",
        ):
            for message in (CAPACITY_MESSAGE, "■ " + CAPACITY_MESSAGE):
                self.assertEqual(failure_kind(code, message), "capacity")

    def test_policy_denials_are_not_reclassified_by_message(self):
        for code in (
            "cyberPolicy",
            "misalignmentPolicyViolation",
            "badRequest",
            "sandboxError",
        ):
            self.assertIsNone(failure_kind(code, CAPACITY_MESSAGE))
        self.assertIsNone(failure_kind("other", "This request mentions capacity"))


class CapacityRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def capacity(self, turn):
        message = completed(code="policyCapacityUnavailable", turn=turn)
        message["params"]["turn"]["error"]["message"] = CAPACITY_MESSAGE
        await self.c.event(message)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.pool = Pool(Path(self.tmp.name), clock=self.clock)
        self.backend = FakeBackend()
        self.c = Controller(
            [FakeAccount("one"), FakeAccount("two")],
            self.backend,
            pool=self.pool,
            clock=self.clock,
            sleep=self.clock.sleep,
            report=lambda _: None,
        )
        self.c.client = FakeClient()
        self.c.last_turn["thread-1"] = {"threadId": "thread-1", "model": "chosen-model"}

    async def asyncTearDown(self):
        if self.c.recovery:
            self.c.recovery.cancel()
            await asyncio.gather(self.c.recovery, return_exceptions=True)
        self.pool.close()
        self.tmp.cleanup()

    async def test_capacity_retries_every_five_minutes_without_cycling_accounts_or_models(
        self,
    ):
        for i in range(4):
            await self.capacity(str(i))
            await self.c.recovery
        self.assertEqual(self.clock.waits, [300, 300, 300, 300])
        self.assertEqual(self.c.index, 0)
        self.assertEqual(self.c.model_for("thread-1"), "chosen-model")
        starts = [p for m, p in self.backend.calls if m == "turn/start"]
        self.assertEqual(len(starts), 4)
        self.assertTrue(all(p["threadId"] == "thread-1" for p in starts))
        self.assertFalse(
            any(
                m in {"thread/resume", "account/login/start"}
                for m, _ in self.backend.calls
            )
        )
        with self.pool.db() as db:
            reasons = [r[0] for r in db.execute("SELECT reason FROM blocks")]
        self.assertFalse(reasons)

    async def test_legacy_message_continues_saved_thread(self):
        message = completed(code="other")
        message["params"]["turn"]["error"]["message"] = CAPACITY_MESSAGE
        await self.c.event(message)
        await self.c.recovery
        self.assertEqual(self.clock.waits, [300])
        self.assertTrue(any(m == "turn/start" for m, _ in self.backend.calls))

    async def test_final_policy_failure_cancels_an_earlier_capacity_retry(self):
        await self.c.event(
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": False,
                    "error": {
                        "codexErrorInfo": "policyCapacityUnavailable",
                        "message": CAPACITY_MESSAGE,
                    },
                },
            }
        )
        await self.c.event(completed(code="cyberPolicy"))
        await asyncio.gather(self.c.recovery, return_exceptions=True)
        self.assertFalse(any(m == "turn/start" for m, _ in self.backend.calls))

    async def test_native_error_and_final_turn_do_not_schedule_two_retries(self):
        params = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "error": {
                "codexErrorInfo": "policyCapacityUnavailable",
                "message": CAPACITY_MESSAGE,
            },
        }
        await self.c.event({"method": "error", "params": {**params, "willRetry": True}})
        self.assertIsNone(self.c.recovery)
        await self.c.event(
            {"method": "error", "params": {**params, "willRetry": False}}
        )
        await self.capacity("turn-1")
        await self.c.recovery
        self.assertEqual(self.clock.waits, [300])
        self.assertEqual(sum(m == "turn/start" for m, _ in self.backend.calls), 1)

    async def test_capacity_fallback_only_uses_explicitly_configured_model(self):
        self.c.preferences["fallback_models"] = ["chosen-fallback"]
        for i in range(4):
            await self.capacity(str(i))
            await self.c.recovery
        self.assertEqual(self.c.model_for("thread-1"), "chosen-fallback")

    async def test_restored_capacity_retry_retains_its_deadline(self):
        await self.c.restore_recovery(
            "thread-1",
            {
                "accepted": True,
                "kind": "capacity",
                "next_wake": 1500,
                "account_label": "one",
                "episode": {"account": 0, "start": 1000, "attempt": 1},
            },
        )
        self.assertEqual(self.clock.waits, [500])
        self.assertEqual(sum(m == "turn/start" for m, _ in self.backend.calls), 1)

    async def test_missing_continuation_baseline_cannot_accept_old_completed_turn_as_new(
        self,
    ):
        async def lost(method, params=None, **kwargs):
            if method == "turn/start":
                raise asyncio.TimeoutError()
            if method == "thread/turns/list":
                return {"data": [{"id": "old", "status": "completed"}]}
            if method == "thread/read":
                return {"thread": {"status": {"type": "idle"}}}
            return {}

        self.backend.call = lost
        await self.c.continue_thread("thread-1", 0)
        self.assertIn("thread-1", self.c.paused_uncertain)
        self.assertFalse(self.c.pending_intents["thread-1"]["accepted"])

    async def test_unrelated_wake_does_not_shorten_service_retry(self):
        self.c.clock = time.time
        self.c.sleep = asyncio.sleep
        self.c.recovery = asyncio.create_task(self.c.recover("thread-1", 0, "capacity"))
        await wait_for(lambda: self.c.episodes.get("thread-1", {}).get("attempt") == 1)
        self.c.wake.set()
        await asyncio.sleep(0.03)
        self.assertFalse(self.c.recovery.done())
        self.assertFalse(any(m == "turn/start" for m, _ in self.backend.calls))
        # Explicit Retry is allowed to override the saved timer.
        await self.c.control("retry", {})
        await asyncio.wait_for(self.c.recovery, 1)
        self.assertTrue(any(m == "turn/start" for m, _ in self.backend.calls))


class DoctorRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_incompatible_native_runtime_is_failure_exit(self):
        import commands

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            Pool(state).close()

            async def absent(*args, **kw):
                raise OSError()

            with (
                patch("compatibility.check", side_effect=ValueError()),
                patch("broker.exchange", absent),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = await commands.doctor(
                    SimpleNamespace(
                        restore_backup=False, self_test=False, backup=False
                    ),
                    state,
                    "fake",
                )
            self.assertEqual(code, 1)
