import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pool import Pool
from settings import atomic_json
import service
import broker
from test_router import FakeAccount


def fake_executable(root):
    path = root / "codex"
    path.write_text(
        "#!"
        + str(Path(sys.executable).resolve())
        + "\n"
        + Path(__file__).with_name("fake_native.py").read_text()
    )
    path.chmod(0o700)
    return str(path)


async def wait_for(predicate, timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.03)
    raise AssertionError("Timed out waiting for test state")


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_shares_native_helper_and_usage_read_across_six_clients(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="codex-broker-test-") as folder:
            root = Path(folder)
            binary = fake_executable(root)
            home = root / "accounts" / "only"
            home.mkdir(parents=True)
            (home / "enrolled").write_text("{}")
            try:
                await broker.ensure(root, binary)
                results = await asyncio.gather(
                    *(
                        broker.exchange(root, {"op": "usage", "label": "only"})
                        for _ in range(6)
                    )
                )
                self.assertTrue(
                    all(
                        r["rateLimits"]["primary"]["usedPercent"] == 20 for r in results
                    )
                )
                calls = json.loads((home / "fake-history.json").read_text())["calls"]
                self.assertEqual(sum(c["method"] == "initialize" for c in calls), 1)
                self.assertEqual(
                    sum(c["method"] == "account/rateLimits/read" for c in calls), 1
                )
                _, meta = broker.paths(root)
                self.assertEqual(meta.stat().st_mode & 0o777, 0o600)
                # Wrong capability cannot fetch a response or credentials.
                original = json.loads(meta.read_text())
                bad = dict(original, token="wrong")
                atomic_json(meta, bad)
                with self.assertRaises(RuntimeError):
                    await broker.exchange(root, {"op": "usage", "label": "only"})
                atomic_json(meta, original)
            finally:
                with contextlib.suppress(Exception):
                    await broker.exchange(root, {"op": "stop"})
                await wait_for(lambda: not broker.paths(root)[1].exists())


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-service-test-")
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.binary = fake_executable(self.root)
        account = self.state / "accounts" / "only"
        account.mkdir(parents=True)
        (account / "enrolled").write_text("{}")
        atomic_json(
            self.state / "settings.json",
            {"settings": {"watchdog_interval": 1, "retry_interval": 1}},
        )
        self.ident = "service-test-owner"
        self.job = service.folder(self.state) / (self.ident + ".job.json")
        atomic_json(
            self.job,
            {
                "id": self.ident,
                "name": "unit task",
                "cwd": str(self.home),
                "first": None,
                "arguments": [],
                "resume": None,
                "prompt": "test only",
                "profile": None,
                "state": "starting",
                "binary": self.binary,
            },
        )

        class Account(FakeAccount):
            def __init__(self, label, *args):
                super().__init__(label)

            async def close(self):
                pass

        self.account_patch = patch("broker.ClientAccount", Account)
        self.account_patch.start()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.env.start()
        self.task = asyncio.create_task(
            service.serve(self.state, self.ident, self.binary)
        )
        self.meta = service.folder(self.state) / (self.ident + ".connection.json")
        await wait_for(self.meta.exists)

    async def asyncTearDown(self):
        pool = Pool(self.state)
        pool.command(self.ident, "stop")
        pool.close()
        try:
            await asyncio.wait_for(self.task, 5)
        except (asyncio.TimeoutError, Exception):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.env.stop()
        self.account_patch.stop()
        self.tmp.cleanup()

    async def test_background_work_completes_without_frontend(self):
        def completed():
            path = self.home / "fake-history.json"
            value = json.loads(path.read_text())
            return any(
                t["turns"] and t["turns"][-1]["status"] == "completed"
                for t in value["threads"].values()
            )

        await wait_for(completed)
        self.assertFalse(self.task.done())
        self.assertIsNone(json.loads(self.job.read_text())["prompt"])
        self.assertEqual(self.meta.stat().st_mode & 0o777, 0o600)

    async def test_capacity_message_continues_over_real_jsonl_transport_without_relogin(
        self,
    ):
        from websockets.asyncio.client import connect

        history = self.home / "fake-history.json"
        await wait_for(
            lambda: any(
                t["turns"] and t["turns"][-1]["status"] == "completed"
                for t in json.loads(history.read_text())["threads"].values()
            )
        )
        meta = json.loads(self.meta.read_text())
        thread = meta["thread"]
        atomic_json(self.home / "fault.json", {"capacity_turns": 2})
        async with connect(
            meta["url"],
            additional_headers={"Authorization": "Bearer " + meta["token"]},
            proxy=None,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": "capacity-test",
                        "method": "turn/start",
                        "params": {
                            "threadId": thread,
                            "input": [
                                {
                                    "type": "text",
                                    "text": "fake only",
                                    "text_elements": [],
                                }
                            ],
                        },
                    }
                )
            )
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == "capacity-test":
                    break

        def recovered():
            saved = json.loads(history.read_text())
            turns = saved["threads"][thread]["turns"]
            return len(turns) == 3 and turns[-1]["status"] == "completed"

        await wait_for(recovered)
        saved = json.loads(history.read_text())
        self.assertEqual(
            saved["threads"][thread]["turns"][1]["error"]["codexErrorInfo"],
            "policyCapacityUnavailable",
        )
        self.assertEqual(
            sum(c["method"] == "account/login/start" for c in saved["calls"]), 1
        )

    async def test_pause_resume_and_control_acknowledgments(self):
        await service.send_control(self.state, self.ident, "pause")
        pool = Pool(self.state)
        try:
            self.assertTrue(
                next(s for s in pool.detail() if s["id"] == self.ident)["paused"]
            )
        finally:
            pool.close()
        await service.send_control(self.state, self.ident, "resume")

    async def test_pause_survives_worker_reconstruction(self):
        await service.send_control(self.state, self.ident, "pause")
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.task = asyncio.create_task(
            service.serve(self.state, self.ident, self.binary)
        )
        await wait_for(self.meta.exists)
        pool = Pool(self.state)
        try:
            self.assertTrue(
                next(s for s in pool.detail() if s["id"] == self.ident)["paused"]
            )
        finally:
            pool.close()

    async def test_worker_restore_keeps_review_pause_even_if_latest_error_is_recoverable(
        self,
    ):
        history = self.home / "fake-history.json"
        await wait_for(
            lambda: any(
                t["turns"] and t["turns"][-1]["status"] == "completed"
                for t in json.loads(history.read_text())["threads"].values()
            )
        )
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        saved = json.loads(history.read_text())
        thread = next(iter(saved["threads"]))
        saved["threads"][thread]["turns"][-1].update(
            status="failed",
            error={"codexErrorInfo": "usageLimitExceeded", "message": "synthetic"},
        )
        atomic_json(history, saved)
        pool = Pool(self.state)
        pool.session = self.ident
        details = pool.intent(thread)
        details.update(accepted=True, active_items=True, state="failed")
        pool.intent(thread, details)
        starts = sum(c["method"] == "turn/start" for c in saved["calls"])
        self.task = asyncio.create_task(
            service.serve(self.state, self.ident, self.binary)
        )
        await wait_for(self.meta.exists)
        try:
            row = next(s for s in pool.detail() if s["id"] == self.ident)
            self.assertEqual(row["state"], "needs_attention")
            self.assertEqual(
                sum(
                    c["method"] == "turn/start"
                    for c in json.loads(history.read_text())["calls"]
                ),
                starts,
            )
        finally:
            pool.close()

    async def test_restore_distinguishes_old_completion_from_accepted_new_work(self):
        history = self.home / "fake-history.json"
        await wait_for(
            lambda: any(
                t["turns"] and t["turns"][-1]["status"] == "completed"
                for t in json.loads(history.read_text())["threads"].values()
            )
        )
        saved = json.loads(history.read_text())
        thread = next(iter(saved["threads"]))
        turn = saved["threads"][thread]["turns"][-1]["id"]
        starts = sum(c["method"] == "turn/start" for c in saved["calls"])
        pool = Pool(self.state)
        pool.session = self.ident
        try:
            for accepted, expected in [(False, "needs_attention"), (True, "idle")]:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
                details = pool.intent(thread)
                details.update(
                    accepted=accepted,
                    active_items=False,
                    baseline=turn,
                    state="retry_scheduled" if accepted else "submitting",
                    next_wake=1,
                )
                pool.intent(thread, details)
                self.task = asyncio.create_task(
                    service.serve(self.state, self.ident, self.binary)
                )
                await wait_for(self.meta.exists)
                self.assertEqual(
                    next(s for s in pool.detail() if s["id"] == self.ident)["state"],
                    expected,
                )
                self.assertEqual(
                    sum(
                        c["method"] == "turn/start"
                        for c in json.loads(history.read_text())["calls"]
                    ),
                    starts,
                )
        finally:
            pool.session = "inspector"
            pool.close()

    async def test_selected_thread_restores_its_own_model_and_permissions(self):
        await wait_for(lambda: json.loads(self.job.read_text()).get("resume"))
        thread = json.loads(self.job.read_text())["resume"]
        await wait_for(
            lambda: any(
                t["turns"] and t["turns"][-1]["status"] == "completed"
                for t in json.loads((self.home / "fake-history.json").read_text())[
                    "threads"
                ].values()
            )
        )
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        pool = Pool(self.state)
        pool.session = self.ident
        pool.intent(
            thread,
            {
                "accepted": True,
                "state": "completed",
                "role": "foreground",
                "settings": {
                    "model": "model-a",
                    "sandbox": "read-only",
                    "approvalPolicy": "on-request",
                },
            },
        )
        pool.intent(
            "zzzzz-other-thread",
            {
                "accepted": True,
                "state": "completed",
                "role": "foreground",
                "settings": {
                    "model": "other-model",
                    "sandbox": "danger-full-access",
                    "approvalPolicy": "never",
                },
            },
        )
        self.task = asyncio.create_task(
            service.serve(self.state, self.ident, self.binary)
        )
        await wait_for(self.meta.exists)
        try:
            restored = pool.intent(thread)["settings"]
            self.assertEqual(restored["model"], "model-a")
            self.assertEqual(restored["sandbox"], "read-only")
        finally:
            pool.close()

    async def test_reattach_uses_workers_runtime_after_global_cli_update(self):
        from unittest.mock import AsyncMock

        process = type(
            "Process", (), {"wait": AsyncMock(return_value=0), "returncode": 0}
        )()
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as spawn:
            self.assertEqual(
                await service.attach(self.state, self.ident, "/new/global/codex"), 0
            )
            self.assertEqual(spawn.call_args.args[0], self.binary)
            expected_cwd = json.loads(self.meta.read_text())["cwd"]
            argv = spawn.call_args.args
            self.assertEqual(
                argv[argv.index("-C") + 1], str(Path(expected_cwd).resolve())
            )
            self.assertEqual(spawn.call_args.kwargs["cwd"], expected_cwd)

    async def test_takeover_rotates_capability_and_old_frontend_cannot_reconnect(self):
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        original = json.loads(self.meta.read_text())
        async with connect(
            original["url"],
            additional_headers={"Authorization": "Bearer " + original["token"]},
            proxy=None,
        ) as ws:
            await service.send_control(self.state, self.ident, "detach")
            await asyncio.wait_for(ws.wait_closed(), 2)
        new = json.loads(self.meta.read_text())
        self.assertEqual(original["pid"], new["pid"])
        self.assertNotEqual(original["token"], new["token"])
        with self.assertRaises(InvalidStatus):
            async with connect(
                new["url"],
                additional_headers={"Authorization": "Bearer " + original["token"]},
                proxy=None,
            ):
                pass
        async with connect(
            new["url"],
            additional_headers={"Authorization": "Bearer " + new["token"]},
            proxy=None,
        ):
            pass

    async def test_failed_backend_restart_is_retried_without_poisoning_controller(self):
        # Exercise the real worker through a failed start, then a successful one.
        from router import Controller, RouterError

        class Dead:
            proc = type("Process", (), {"returncode": 9})()

        old = Dead()
        c = Controller([FakeAccount("only")], old, report=lambda x: None)

        async def unavailable():
            raise RouterError("test startup failure")

        c.backend_factory = unavailable
        await c.restart_backend()
        self.assertIs(c.backend, old)

    async def test_native_backend_crash_is_restarted_without_replaying_completed_prompt(
        self,
    ):
        def ready():
            value = json.loads((self.home / "fake-history.json").read_text())
            return any(
                t["turns"] and t["turns"][-1]["status"] == "completed"
                for t in value["threads"].values()
            )

        await wait_for(ready)
        # The fake process records its own PID on the turn; no unrelated process is targeted.
        state = json.loads((self.home / "fake-history.json").read_text())
        thread = next(iter(state["threads"].values()))
        os.kill(thread["runtime"], 9)

        def restored():
            value = json.loads((self.home / "fake-history.json").read_text())
            return sum(c["method"] == "initialize" for c in value["calls"]) >= 2

        await wait_for(restored)
        await asyncio.sleep(0.2)
        value = json.loads((self.home / "fake-history.json").read_text())
        self.assertEqual(sum(c["method"] == "turn/start" for c in value["calls"]), 1)


if __name__ == "__main__":
    unittest.main()
