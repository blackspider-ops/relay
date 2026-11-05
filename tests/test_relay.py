import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import broker
import commands
from pool import Pool
from router import Controller, parser, RouterError, selected_binary
import service
from settings import atomic_json
from test_router import FakeAccount, FakeBackend, FakeClient


class ModelRules(unittest.IsolatedAsyncioTestCase):
    async def test_model_change_routes_to_an_eligible_account(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(Path(directory))
            pool.assign("one")
            pool.set_options("one", models=["old-model"])
            pool.set_options("two", models=["new-model"])
            backend = FakeBackend()
            c = Controller(
                [FakeAccount("one"), FakeAccount("two")],
                backend,
                pool=pool,
                report=lambda _: None,
            )
            c.client = FakeClient()
            try:
                await c.forward(
                    {
                        "id": 1,
                        "method": "turn/start",
                        "params": {
                            "threadId": "thread",
                            "model": "new-model",
                            "input": [],
                        },
                    },
                    {},
                )
                self.assertEqual(c.account.label, "two")
                self.assertTrue(any(m == "turn/start" for m, _ in backend.calls))
            finally:
                pool.close()

    async def test_disallowed_model_is_not_submitted_when_no_account_is_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(Path(directory))
            pool.set_options("one", models=["allowed"])
            backend = FakeBackend()
            c = Controller(
                [FakeAccount("one")], backend, pool=pool, report=lambda _: None
            )
            c.client = FakeClient()
            try:
                await c.forward(
                    {
                        "id": 1,
                        "method": "turn/start",
                        "params": {
                            "threadId": "thread",
                            "model": "disallowed",
                            "input": [],
                        },
                    },
                    {},
                )
                self.assertFalse(any(m == "turn/start" for m, _ in backend.calls))
                self.assertIn("error", c.client.messages[-1])
            finally:
                pool.close()


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    def test_helper_does_not_require_global_codex_when_runtime_is_provided(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("router.codex_path", side_effect=RouterError("not installed")):
                args = parser().parse_args(["_broker", "--binary", "/explicit/runtime"])
                self.assertEqual(
                    selected_binary(args, Path(directory)), "/explicit/runtime"
                )

    def test_saved_worker_and_metadata_commands_survive_missing_global_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            atomic_json(
                service.folder(state) / "saved.job.json",
                {"binary": "/saved/native/runtime"},
            )
            with patch("router.codex_path", side_effect=RouterError("not installed")):
                self.assertEqual(
                    selected_binary(parser().parse_args(["_serve", "saved"]), state),
                    "/saved/native/runtime",
                )
                self.assertIsNone(
                    selected_binary(parser().parse_args(["status"]), state)
                )

    async def test_compatible_older_coordinator_is_reused_without_interrupting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "broker.exchange",
                    new=AsyncMock(return_value={"revision": "older-build"}),
                ) as exchange,
                patch("broker.spawn") as spawn,
            ):
                await broker.ensure(Path(directory), "fake-native")
                self.assertEqual(exchange.await_count, 1)
                spawn.assert_not_called()

    def test_failed_sessions_are_visible_without_exposing_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            atomic_json(
                service.folder(state) / "failed.job.json",
                {
                    "id": "failed",
                    "name": "Project",
                    "state": "needs_attention",
                    "prompt": "PRIVATE_PROMPT",
                },
            )
            result = commands.snapshot(state)
            self.assertEqual(result["sessions"][0]["state"], "needs_attention")
            self.assertNotIn("PRIVATE_PROMPT", json.dumps(result))

    def test_restart_refuses_a_live_session(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            pool = Pool(state)
            pool.session = "live"
            pool.assign("one")
            atomic_json(
                service.folder(state) / "live.job.json",
                {"id": "live", "cwd": directory, "state": "running"},
            )
            try:
                with self.assertRaises(RouterError):
                    service.restart(state, "live")
            finally:
                pool.close()

    def test_internal_helpers_are_not_in_public_help(self):
        help_text = parser().format_help()
        self.assertNotIn("_supervise", help_text)
        self.assertNotIn("_broker", help_text)
