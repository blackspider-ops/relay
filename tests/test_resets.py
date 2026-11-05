"""Earned-reset tests use synthetic accounts and never redeem a real credit."""

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import commands
from pool import Pool
from router import Account, RouterError, RemoteError, parse_arguments
from settings import atomic_json
import resets


class ResetService:
    def __init__(self):
        self.count = 2
        self.blocked = True
        self.calls = []
        self.completed = set()
        self.lose_reply = False
        self.fail_refresh = False
        self.residual_limit = False
        self.outcome = None
        self.reject = False

    async def call(self, method, params=None):
        await asyncio.sleep(0)
        if method == "account/rateLimits/read":
            if self.fail_refresh and self.completed:
                raise asyncio.TimeoutError()
            return {
                "ordinaryUsageAllowed": not self.blocked,
                "rateLimits": {
                    "primary": {
                        "usedPercent": 100 if self.blocked else 0,
                        "resetsAt": time.time() + 3600,
                    }
                },
                "rateLimitResetCredits": {
                    "availableCount": self.count,
                    "credits": None,
                },
            }
        if method != "account/rateLimitResetCredit/consume":
            raise AssertionError(method)
        if self.reject:
            raise RemoteError({"code": -32601})
        key = params["idempotencyKey"]
        self.calls.append(key)
        if key in self.completed:
            return {"outcome": "alreadyRedeemed"}
        if self.outcome in {"noCredit", "nothingToReset"}:
            if self.outcome == "noCredit":
                self.count = 0
            return {"outcome": self.outcome}
        self.completed.add(key)
        self.count -= 1
        self.blocked = self.residual_limit
        if self.lose_reply:
            self.lose_reply = False
            raise asyncio.TimeoutError()
        return {"outcome": self.outcome or "reset"}


class ResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="relay-reset-test-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "accounts" / "one"
        self.home.mkdir(parents=True)
        atomic_json(self.home / "enrolled", {"identity": "fake-account-A"})
        self.pool = Pool(self.root)
        self.service = ResetService()
        service = self.service

        class TestAccount(Account):
            async def start(self):
                self.rpc = service

            async def close(self):
                self.rpc = None

        self.patch = patch("router.Account", TestAccount)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.pool.close()
        self.tmp.cleanup()

    async def test_success_refreshes_allowance_and_only_lifts_verified_quota_blocks(
        self,
    ):
        self.pool.mark("one", reason="quota")
        self.pool.mark("one", "other-model", reason="auth")
        self.pool.intent("thread", {"review_required": True})
        self.pool.touch(paused=True)
        result = await resets.consume(self.root, "one", "fake")
        self.assertEqual(result, {"outcome": "reset", "refreshed": True})
        self.assertEqual(resets.available(self.pool.cached("one")), 1)
        with self.pool.db() as db:
            self.assertEqual(
                db.execute("SELECT scope,reason FROM blocks").fetchall(),
                [("other-model", "auth")],
            )
            details = json.loads(
                db.execute(
                    "SELECT details FROM session_details WHERE id=?",
                    (self.pool.session,),
                ).fetchone()[0]
            )
        self.assertTrue(details["paused"])
        self.assertTrue(self.pool.intent("thread")["review_required"])
        self.assertEqual(
            (self.home / "reset-attempt.json").stat().st_mode & 0o777, 0o600
        )

    async def test_lost_reply_reuses_pending_key_even_if_next_click_has_a_new_key(self):
        self.service.lose_reply = True
        with self.assertRaisesRegex(RouterError, "same pending request"):
            await resets.consume(self.root, "one", "fake", str(uuid.uuid4()))
        self.assertTrue(resets.pending(self.home))
        account = commands.snapshot(self.root)["accounts"][0]
        self.assertTrue(account["reset_pending"])
        self.assertIsNone(account["reset_credits"])
        result = await resets.consume(self.root, "one", "fake", str(uuid.uuid4()))
        self.assertEqual(result["outcome"], "alreadyRedeemed")
        self.assertEqual(len(set(self.service.calls)), 1)
        self.assertEqual(self.service.count, 1)
        self.assertFalse(resets.pending(self.home))

    async def test_concurrent_duplicate_request_is_redeemed_once(self):
        key = str(uuid.uuid4())
        results = await asyncio.gather(
            *(resets.consume(self.root, "one", "fake", key) for _ in range(2))
        )
        self.assertEqual({r["outcome"] for r in results}, {"reset", "alreadyRedeemed"})
        self.assertEqual(self.service.calls, [key])

    async def test_success_with_failed_refresh_does_not_claim_updated_limits(self):
        self.service.fail_refresh = True
        key = str(uuid.uuid4())
        self.pool.mark("one", reason="quota")
        result = await resets.consume(self.root, "one", "fake", key)
        self.assertFalse(result["refreshed"])
        self.assertIn("could not be refreshed", resets.message(result))
        self.assertIsNone(self.pool.cached("one"))
        self.assertGreater(self.pool.deadline("one"), time.time())
        retry = await resets.consume(self.root, "one", "fake", key)
        self.assertEqual(retry["outcome"], "alreadyRedeemed")
        self.assertEqual(len(self.service.calls), 1)

    async def test_remaining_limit_is_retained_after_a_reset(self):
        self.service.residual_limit = True
        self.pool.mark("one", reason="quota")
        await resets.consume(self.root, "one", "fake")
        self.assertGreater(self.pool.deadline("one"), time.time())

    async def test_no_credit_or_nothing_to_reset_are_reported_without_success_claim(
        self,
    ):
        for outcome in ("noCredit", "nothingToReset"):
            self.service.outcome = outcome
            self.service.count = 2
            result = await resets.consume(self.root, "one", "fake")
            self.assertEqual(result["outcome"], outcome)
            self.assertIn("no reset was used", resets.message(result))
        self.service.count = 0
        self.service.calls.clear()
        result = await resets.consume(self.root, "one", "fake")
        self.assertEqual(result["outcome"], "noCredit")
        self.assertFalse(self.service.calls)

    async def test_unknown_outcome_remains_pending_and_unsupported_method_is_actionable(
        self,
    ):
        self.service.outcome = "newUnknownOutcome"
        with self.assertRaisesRegex(RouterError, "not recognized"):
            await resets.consume(self.root, "one", "fake")
        self.assertTrue(resets.pending(self.home))
        self.service.reject = True
        with self.assertRaisesRegex(RouterError, "Update Codex"):
            await resets.consume(self.root, "one", "fake")
        self.assertFalse(resets.pending(self.home))

    async def test_cli_requires_confirmation_before_starting_redemption(self):
        args = parse_arguments(["account", "reset", "one"])
        with patch("resets.consume", side_effect=AssertionError("Unconfirmed reset")):
            with self.assertRaisesRegex(RouterError, "--yes"):
                await commands.manage(args, self.root, "fake")

    async def test_real_jsonl_helper_redeems_and_refreshes_synthetic_account(self):
        from test_services import fake_executable

        self.patch.stop()
        binary = fake_executable(self.root)
        result = await resets.consume(self.root, "one", binary)
        self.assertEqual(result, {"outcome": "reset", "refreshed": True})
        history = json.loads((self.home / "fake-history.json").read_text())
        self.assertEqual(len(history["reset_keys"]), 1)
        self.assertEqual(history["reset_credit_count"], 0)
        self.assertEqual(
            sum(c["method"] == "account/rateLimits/read" for c in history["calls"]), 2
        )

    def test_available_count_is_authoritative_and_invalid_values_stay_unknown(self):
        self.assertEqual(
            resets.available(
                {"rateLimitResetCredits": {"availableCount": 3, "credits": []}}
            ),
            3,
        )
        for count in (-1, True, "2", 1.5, None):
            self.assertIsNone(
                resets.available({"rateLimitResetCredits": {"availableCount": count}})
            )
        with self.assertRaises(ValueError):
            resets.update_allowance(self.pool, "one", {})


if __name__ == "__main__":
    unittest.main()
