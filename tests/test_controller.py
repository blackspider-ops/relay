import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from router import Controller, RPC, RouterError, parser, labels
from launch import translate, backend_arguments
from pool import Pool
from test_router import FakeAccount, FakeBackend, FakeClient
from test_pool import Clock
import settings


class LaunchTests(unittest.TestCase):
    def test_resume_yolo_is_backend_override_not_remote_frontend_flag(self):
        args, overrides = translate(["--", "resume", "--last", "--yolo"])
        self.assertEqual(args, ["resume", "--last"])
        self.assertEqual(
            overrides, {"approvalPolicy": "never", "sandbox": "danger-full-access"}
        )

    def test_explicit_permission_modes_translate(self):
        args, value = translate(
            ["resume", "-s", "workspace-write", "--ask-for-approval=on-request"]
        )
        self.assertEqual(args, ["resume"])
        self.assertEqual(value["sandbox"], "workspace-write")

    def test_prompt_after_separator_is_not_parsed_as_permissions(self):
        args, value = translate(["resume", "--", "--yolo"])
        self.assertEqual(args, ["resume", "--", "--yolo"])
        self.assertEqual(value, {})

    def test_config_profile_forwarded_to_backend(self):
        self.assertEqual(
            backend_arguments(
                ["resume", "-p", "work", "-c", 'model_reasoning_effort="high"']
            ),
            ["-p", "work", "-c", 'model_reasoning_effort="high"'],
        )

    def test_setup_supports_one_or_arbitrary_count(self):
        for count in [1, 4, 7, 32]:
            self.assertEqual(
                parser().parse_args(["setup", "--count", str(count)]).count, count
            )
        self.assertEqual(
            parser().parse_args(["setup", "work", "personal"]).labels,
            ["work", "personal"],
        )

    def test_account_discovery_not_fixed_to_four(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for i in range(17):
                path = root / "accounts" / f"person{i}"
                path.mkdir(parents=True)
                (path / "enrolled").write_text("{}")
            self.assertEqual(len(labels(root)), 17)


class RegistryTests(unittest.TestCase):
    def test_maintenance_and_reservations_cannot_race(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            one = Pool(root)
            two = Pool(root)
            self.assertTrue(one.reserve("a"))
            self.assertFalse(two.begin_maintenance("a"))
            one.release_reservation()
            self.assertTrue(two.begin_maintenance("a"))
            self.assertFalse(one.reserve("a"))
            two.set_options("a", maintenance=False, enabled=False)
            self.assertFalse(one.reserve("a"))

    def test_interrupted_maintenance_does_not_permanently_disable_account(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = Pool(Path(folder))
            pool.set_options(
                "a",
                maintenance=True,
                maintenance_pid=os.getpid(),
                maintenance_birth="old process generation",
            )
            self.assertFalse(pool.options("a")["maintenance"])
            self.assertTrue(pool.reserve("a"))

    def test_inflight_reservation_is_renewed_until_assignment(self):
        with tempfile.TemporaryDirectory() as folder:
            now = [1000]
            root = Path(folder)
            one = Pool(root, clock=lambda: now[0])
            two = Pool(root, clock=lambda: now[0])
            one.reserve("a", ttl=120)
            now[0] = 1100
            one.renew_reservation(120)
            now[0] = 1150
            self.assertFalse(two.reserve("a", limit=1))
            one.assign("a")
            self.assertFalse(two.reserve("a", limit=1))

    def test_metadata_corruption_restore_retains_replaced_database(self):
        import sqlite3
        from metadata import restore_backup

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pool = Pool(root)
            pool.set_options("one", priority=7)
            source = sqlite3.connect(root / "pool.sqlite")
            backup = sqlite3.connect(root / "pool.backup.sqlite")
            source.backup(backup)
            source.close()
            backup.close()
            (root / "pool.sqlite").write_bytes(b"synthetic corruption")
            saved = Path(restore_backup(root))
            self.assertEqual(
                (saved / "pool.sqlite").read_bytes(), b"synthetic corruption"
            )
            self.assertEqual(Pool(root).options("one")["priority"], 7)

    def test_restore_refuses_live_worker_without_changing_database(self):
        import sqlite3
        from metadata import restore_backup

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pool = Pool(root)
            pool.assign("one")
            source = sqlite3.connect(root / "pool.sqlite")
            backup = sqlite3.connect(root / "pool.backup.sqlite")
            source.backup(backup)
            source.close()
            backup.close()
            with self.assertRaises(ValueError):
                restore_backup(root)
            self.assertEqual(len(pool.detail()), 1)

    def test_replacing_identity_clears_all_model_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = Pool(Path(folder))
            pool.mark("one", "model-a")
            pool.mark("one", "model-b")
            pool.mark("one")
            pool.reset_identity("one")
            self.assertEqual(pool.deadline("one", "model-a"), 0)
            self.assertEqual(pool.deadline("one", "model-b"), 0)

    def test_reservations_are_atomic_and_expire(self):
        with tempfile.TemporaryDirectory() as folder:
            now = [1000]
            one = Pool(Path(folder), clock=lambda: now[0])
            two = Pool(Path(folder), clock=lambda: now[0])
            self.assertTrue(one.reserve("a", expected_load=0, ttl=10))
            self.assertFalse(two.reserve("a", expected_load=0, ttl=10))
            now[0] = 1011
            self.assertTrue(two.reserve("a", expected_load=0))

    def test_process_birth_prevents_pid_reuse_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            one = Pool(Path(folder))
            one.assign("a")
            one.claim("t")
            with one.db() as db:
                db.execute(
                    "UPDATE session_details SET birth='wrong process generation' WHERE id=?",
                    (one.session,),
                )
            two = Pool(Path(folder))
            self.assertTrue(two.claim("t"))

    def test_configuration_validation_and_profiles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings.edit(root, "retry_interval", "10", "quick")
            self.assertEqual(settings.load(root, profile="quick")["retry_interval"], 10)
            self.assertEqual(settings.load(root)["retry_interval"], 300)
            with self.assertRaises(ValueError):
                settings.edit(root, "retry_interval", "-1")
            with self.assertRaises(ValueError):
                settings.edit(root, "not_a_setting", "1")


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pool = Pool(self.root)
        self.backend = FakeBackend()
        self.c = Controller(
            [FakeAccount("one"), FakeAccount("two")],
            self.backend,
            pool=self.pool,
            report=lambda x: None,
        )
        self.c.client = FakeClient()

    async def asyncTearDown(self):
        if self.c.recovery:
            self.c.recovery.cancel()
            await asyncio.gather(self.c.recovery, return_exceptions=True)
        self.pool.close()
        self.tmp.cleanup()

    async def test_sticky_model_does_not_revert_after_partial_turn(self):
        self.c.thread_settings["t"] = {"model": "old"}
        await self.c.forward(
            {
                "id": 1,
                "method": "turn/start",
                "params": {"threadId": "t", "model": "new", "input": []},
            },
            {},
        )
        await self.c.forward(
            {"id": 2, "method": "turn/start", "params": {"threadId": "t", "input": []}},
            {},
        )
        self.assertEqual(self.c.model_for("t"), "new")

    async def test_resume_yolo_injected_into_server_resume(self):
        self.c.launch_overrides = {
            "sandbox": "danger-full-access",
            "approvalPolicy": "never",
        }
        await self.c.forward(
            {"id": 1, "method": "thread/resume", "params": {"threadId": "t"}}, {}
        )
        params = next(p for m, p in self.backend.calls if m == "thread/resume")
        self.assertEqual(params["sandbox"], "danger-full-access")
        self.assertEqual(params["approvalPolicy"], "never")

    async def test_readonly_native_conversation_is_not_claimed(self):
        async def read_only(method, params=None, **kw):
            return {"thread": {"id": "t", "canAcceptDirectInput": False}}

        self.backend.call = read_only
        await self.c.forward(
            {"id": 1, "method": "thread/resume", "params": {"threadId": "t"}}, {}
        )
        self.assertIn("error", self.c.client.messages[-1])
        self.assertIsNone(self.pool.owner("t"))

    async def test_single_account_waits_until_reset_then_continues(self):
        clock = Clock()
        self.c.clock = clock
        self.c.sleep = clock.sleep
        self.pool.clock = clock
        self.c.accounts = [FakeAccount("one")]
        self.c.last_turn["t"] = {"model": "m"}
        calls = [0]

        async def usage(force=False):
            calls[0] += 1
            if clock() < 1500:
                return {
                    "ordinaryUsageAllowed": False,
                    "rateLimits": {"primary": {"usedPercent": 100, "resetsAt": 1500}},
                }
            return {
                "ordinaryUsageAllowed": True,
                "rateLimits": {"primary": {"usedPercent": 0, "resetsAt": 3000}},
            }

        self.c.accounts[0].usage = usage
        await self.c.recover("t", 0, "quota")
        self.assertEqual(clock.waits, [502])
        self.assertEqual(calls[0], 2)
        self.assertEqual(self.c.index, 0)
        self.assertTrue(any(m == "turn/start" for m, _ in self.backend.calls))

    async def test_dropped_ack_reconciles_completed_turn_without_replay(self):
        async def read(method, params=None, **kw):
            if method == "thread/turns/list":
                return {"data": [{"id": "new", "status": "completed"}]}
            if method == "thread/read":
                return {"thread": {"status": {"type": "idle"}}}
            raise AssertionError("No turn should be submitted during reconciliation")

        self.backend.call = read
        self.c.last_turn["t"] = {"threadId": "t"}
        await self.c.uncertain("t", 0, "old")
        self.assertFalse(self.c.paused_uncertain)

    async def test_unknown_acceptance_is_not_blindly_replayed(self):
        clock = Clock()
        self.c.clock = clock
        self.c.sleep = clock.sleep
        self.c.last_turn["t"] = {}
        await self.c.uncertain("t", 0, "old")
        self.assertIn("t", self.c.paused_uncertain)
        self.assertFalse(any(m == "turn/start" for m, _ in self.backend.calls))
        self.assertEqual(clock.waits, [300, 300, 300])

    async def test_disabled_account_skipped(self):
        self.pool.set_options("two", enabled=False)
        index, _ = await self.c.choose("m")
        self.assertEqual(index, 0)

    async def test_session_cap_is_honored(self):
        other = Pool(self.root)
        other.assign("two")
        self.pool.set_options("two", max_sessions=1)
        try:
            index, _ = await self.c.choose("m")
            self.assertEqual(index, 0)
        finally:
            other.close()

    async def test_manual_switch_honors_caps_and_disabled_accounts(self):
        self.pool.set_options("two", enabled=False)
        with self.assertRaises(RouterError):
            await self.c.control("switch", {"account": "two"})
        self.pool.set_options("two", enabled=True, max_sessions=1)
        other = Pool(self.root)
        other.assign("two")
        try:
            with self.assertRaises(RouterError):
                await self.c.control("switch", {"account": "two"})
            self.assertEqual(self.c.index, 0)
        finally:
            other.close()

    async def test_restored_retry_keeps_original_deadline_and_attempt(self):
        clock = Clock()
        self.c.clock = clock
        self.c.sleep = clock.sleep
        self.c.last_turn["t"] = {"threadId": "t"}
        await self.c.restore_recovery(
            "t",
            {
                "accepted": True,
                "kind": "transient",
                "next_wake": 1250,
                "account_label": "one",
                "episode": {"account": 99, "start": 900, "attempt": 2},
            },
        )
        self.assertEqual(clock.waits, [250])
        self.assertEqual(
            self.c.episodes["t"], {"account": 0, "start": 900, "attempt": 2}
        )
        self.assertEqual(sum(m == "turn/start" for m, _ in self.backend.calls), 1)

    async def test_restored_uncertain_action_pauses_without_replay(self):
        await self.c.restore_recovery("t", {"accepted": True, "active_items": True})
        self.assertIn("t", self.c.paused_uncertain)
        self.assertFalse(any(m == "turn/start" for m, _ in self.backend.calls))

    async def test_pending_approval_replayed_once_after_resume(self):
        async def read(method, params=None, **kw):
            return {"thread": {"id": "t"}}

        self.backend.call = read
        self.c.client = None
        pending = {
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "t"},
        }
        await self.c.event(pending)
        self.c.client = FakeClient()
        for i in range(2):
            await self.c.forward(
                {"id": i, "method": "thread/resume", "params": {"threadId": "t"}}, {}
            )
        self.assertEqual(self.c.client.messages.count(pending), 1)
        await self.c.forward(
            {"id": "approval-1", "result": {"decision": "decline"}}, {}
        )
        self.assertFalse(self.c.pending_server_requests)
        self.assertEqual(self.backend.sent[-1]["result"]["decision"], "decline")

    async def test_model_fallback_is_opt_in_and_does_not_loop(self):
        self.c.last_turn["t"] = {"threadId": "t", "model": "original"}
        self.c.preferences["fallback_models"] = ["fallback"]
        await self.c.recover("t", 0, "model")
        self.assertEqual(self.c.model_for("t"), "fallback")
        await self.c.recover("t", 0, "model")
        self.assertEqual(sum(m == "turn/start" for m, _ in self.backend.calls), 1)

    async def test_six_simultaneous_quota_handoffs_obey_shared_capacity(self):
        controllers = []
        registries = []

        class YieldingAccount(FakeAccount):
            async def usage(self, force=False):
                await asyncio.sleep(0.001)
                return await super().usage(force)

        for i in range(6):
            pool = Pool(self.root)
            registries.append(pool)
            pool.assign("spent")
            c = Controller(
                [YieldingAccount("spent", blocked=True)]
                + [YieldingAccount("spare" + str(j)) for j in range(3)],
                FakeBackend(),
                pool=pool,
                report=lambda x: None,
            )
            c.client = FakeClient()
            c.last_turn["t" + str(i)] = {"threadId": "t" + str(i)}
            controllers.append(c)
        for j in range(3):
            self.pool.set_options("spare" + str(j), max_sessions=2)
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        c.recover("t" + str(i), 0, "quota")
                        for i, c in enumerate(controllers)
                    )
                ),
                12,
            )
            loads = self.pool.loads()
            self.assertEqual(
                [loads.get("spare" + str(j), 0) for j in range(3)], [2, 2, 2]
            )
            self.assertTrue(
                all(
                    sum(m == "turn/start" for m, _ in c.backend.calls) == 1
                    for c in controllers
                )
            )
        finally:
            for pool in registries:
                pool.close()


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        script = self.home / "codex"
        script.write_text(
            "#!"
            + str(Path(__import__("sys").executable).resolve())
            + "\n"
            + Path(__file__).with_name("fake_native.py").read_text()
        )
        script.chmod(0o700)
        self.binary = str(script)
        self.rpc = await RPC.start(self.binary, self.home)
        await self.rpc.initialize()

    async def asyncTearDown(self):
        await self.rpc.close()
        self.tmp.cleanup()

    async def completed_turn(self, thread):
        async with asyncio.timeout(5):
            while True:
                result = await self.rpc.call(
                    "thread/turns/list", {"threadId": thread, "limit": 1}
                )
                if result["data"] and result["data"][0]["status"] == "completed":
                    return result["data"][0]
                await asyncio.sleep(0.02)

    async def test_actual_jsonl_pipe_roundtrip_and_persisted_history(self):
        result = await self.rpc.call("thread/start", {"cwd": str(self.home)})
        thread = result["thread"]["id"]
        await self.rpc.call("turn/start", {"threadId": thread, "input": []})
        result = await self.completed_turn(thread)
        self.assertEqual(result["status"], "completed")

    async def test_watchdog_clears_stale_active_thread_from_native_status(self):
        result = await self.rpc.call("thread/start", {"cwd": str(self.home)})
        thread = result["thread"]["id"]
        c = Controller([FakeAccount("one")], self.rpc, report=lambda x: None)
        c.client = FakeClient()
        c.active_threads.add(thread)
        await c.reconcile(thread)
        self.assertNotIn(thread, c.active_threads)

    async def test_background_socket_disconnect_does_not_cancel_task(self):
        from websockets.asyncio.client import connect
        from websockets.asyncio.server import serve
        from router import transport_guard

        c = Controller([FakeAccount("one")], self.rpc, report=lambda x: None)
        c.background = True
        self.rpc.handler = c.event
        result = await self.rpc.call("thread/start", {"cwd": str(self.home)})
        thread = result["thread"]["id"]
        async with serve(
            lambda ws: c.connect(ws, {}),
            "127.0.0.1",
            0,
            process_request=transport_guard("test"),
        ) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with connect(
                url, additional_headers={"Authorization": "Bearer test"}, proxy=None
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "turn/start",
                            "params": {"threadId": thread, "input": []},
                        }
                    )
                )
                while True:
                    value = json.loads(await ws.recv())
                    if value.get("id") == 1:
                        break
            latest = await self.completed_turn(thread)
            self.assertEqual(c.user_generation, 1)
            self.assertEqual(latest["status"], "completed")


if __name__ == "__main__":
    unittest.main()
