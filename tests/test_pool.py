"""Schedules use an injected clock. No real five-minute delays or accounts."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

from pool import Pool, file_lock, reset_time, failure_kind
from router import Controller, RouterError, RPC
from test_router import FakeAccount, FakeBackend, FakeClient, completed
import compatibility


class Clock:
    def __init__(self):
        self.now = 1000
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.pool = Pool(Path(self.tmp.name), clock=self.clock)
        self.accounts = [FakeAccount("one"), FakeAccount("two"), FakeAccount("three")]
        self.backend = FakeBackend()
        self.controller = Controller(
            self.accounts,
            self.backend,
            report=lambda x: None,
            pool=self.pool,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.controller.client = FakeClient()
        self.controller.last_turn["thread-1"] = {"model": "model-x"}

    async def asyncTearDown(self):
        if self.controller.recovery:
            self.controller.recovery.cancel()
            await asyncio.gather(self.controller.recovery, return_exceptions=True)
        self.pool.close()
        self.tmp.cleanup()

    async def fail(self, code="internalServerError", turn="turn"):
        await self.controller.event(completed(code=code, turn=turn))
        await self.controller.recovery

    async def test_same_account_retry_at_5_10_15_minutes_then_switch(self):
        for attempt in range(3):
            await self.fail(turn=str(attempt))
            self.assertEqual(self.controller.index, 0)
            self.assertEqual(self.clock.now, 1000 + 300 * (attempt + 1))
        self.assertEqual(sum(name == "turn/start" for name, _ in self.backend.calls), 3)
        self.assertFalse(
            any(name == "account/login/start" for name, _ in self.backend.calls)
        )
        await self.fail(turn="last")
        self.assertNotEqual(self.controller.index, 0)
        self.assertEqual(self.clock.now, 1900)

    async def test_revoked_login_retries_then_changes_account(self):
        async def revoked(refresh=False):
            raise RouterError("revoked")

        self.accounts[0].credentials = revoked
        await self.fail("unauthorized")
        self.assertEqual(self.clock.waits, [300, 300, 300])
        self.assertEqual(self.controller.index, 1)

    async def test_success_clears_failure_episode(self):
        await self.fail()
        await self.controller.event(completed(status="completed", turn="success"))
        self.assertFalse(self.controller.episodes)
        await self.fail(turn="new")
        self.assertEqual(self.controller.episodes["thread-1"]["attempt"], 1)

    async def test_cancel_pending_retry_no_continuation(self):
        waiting = asyncio.Event()

        async def sleep(seconds):
            waiting.set()
            await asyncio.Future()

        self.controller.sleep = sleep
        await self.controller.event(completed(code="internalServerError"))
        await waiting.wait()
        await self.controller.forward(
            {"id": 9, "method": "turn/interrupt", "params": {"threadId": "thread-1"}},
            {},
        )
        await asyncio.sleep(0)
        self.assertTrue(self.controller.recovery.cancelled())
        self.assertFalse(any(m == "turn/start" for m, _ in self.backend.calls))

    async def test_all_exhausted_wait_until_earliest_account_reset_no_polling(self):
        for label, reset in [("one", 5000), ("two", 3000), ("three", 7000)]:
            self.pool.mark(
                label,
                "model-x",
                snapshot={
                    "ordinaryUsageAllowed": False,
                    "rateLimits": {"primary": {"usedPercent": 100, "resetsAt": reset}},
                },
            )
        index, deadline = await self.controller.choose("model-x")
        self.assertIsNone(index)
        self.assertEqual(deadline, 3002)
        self.assertEqual(sum(a.checks for a in self.accounts), 0)
        self.clock.now = deadline
        index, _ = await self.controller.choose("model-x")
        self.assertEqual(index, 1)
        self.assertEqual([a.checks for a in self.accounts], [0, 1, 0])

    async def test_reset_must_be_rechecked_not_assumed_available(self):
        self.pool.mark(
            "two",
            "model-x",
            snapshot={
                "rateLimits": {
                    "rateLimitReachedType": "rate_limit_reached",
                    "primary": {"usedPercent": 100, "resetsAt": 1100},
                }
            },
        )
        self.accounts[1].blocked = True
        self.clock.now = 1102
        self.pool.mark("one", "model-x", delay=900)
        self.pool.mark("three", "model-x", delay=900)
        index, deadline = await self.controller.choose("model-x")
        self.assertIsNone(index)
        self.assertGreater(deadline, self.clock.now)

    async def test_unknown_reset_uses_five_minutes_not_busy_cycle(self):
        for a in self.accounts:
            a.blocked = True
        index, until = await self.controller.choose("model-x")
        self.assertIsNone(index)
        self.assertEqual(until, 1300)
        for _ in range(10):
            await self.controller.choose("model-x")
        self.assertEqual(sum(a.checks for a in self.accounts), 3)

    async def test_cancellation_during_login_finishes_identity_transition(self):
        started, finish = asyncio.Event(), asyncio.Event()
        original = self.backend.call

        async def delayed(method, params=None, **kwargs):
            if method == "account/login/start":
                started.set()
                await finish.wait()
            return await original(method, params, **kwargs)

        self.backend.call = delayed
        task = asyncio.create_task(self.controller.login(1))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.controller.index, 1)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.pool.loads().get("two"), 1)

    async def test_same_conversation_cannot_run_in_two_terminals(self):
        other = Pool(Path(self.tmp.name))
        try:
            self.assertTrue(other.claim("thread-1"))
            await self.controller.forward(
                {
                    "id": 22,
                    "method": "turn/start",
                    "params": {"threadId": "thread-1", "input": []},
                },
                {},
            )
            self.assertEqual(self.backend.calls, [])
            self.assertIn("error", self.controller.client.messages[-1])
        finally:
            other.close()


class PoolTests(unittest.TestCase):
    def test_both_short_and_weekly_windows_must_reset(self):
        snapshot = {
            "rateLimits": {
                "primary": {"usedPercent": 100, "resetsAt": 2000},
                "secondary": {"usedPercent": 100, "resetsAt": 9000},
            }
        }
        self.assertEqual(reset_time(snapshot, now=1000), 9002)
        snapshot["rateLimits"]["secondary"]["usedPercent"] = 42
        self.assertEqual(reset_time(snapshot, now=1000), 2002)

    def test_missing_or_past_reset_is_not_invented(self):
        for value in [None, 10]:
            self.assertIsNone(
                reset_time(
                    {
                        "rateLimits": {
                            "primary": {"usedPercent": 100, "resetsAt": value}
                        }
                    },
                    now=1000,
                )
            )

    def test_all_retry_classes_and_no_policy_circumvention(self):
        for code in [
            "usageLimitExceeded",
            "unauthorized",
            "internalServerError",
            "rateLimitExceeded",
        ]:
            self.assertIsNotNone(failure_kind(code))
        for code in [
            "cyberPolicy",
            "misalignmentPolicyViolation",
            "badRequest",
            "sandboxError",
            None,
        ]:
            self.assertIsNone(failure_kind(code))
        for status in [None, 408, 429, 500, 503]:
            self.assertEqual(
                failure_kind({"httpConnectionFailed": {"httpStatusCode": status}}),
                "transient",
            )
        self.assertEqual(
            failure_kind({"httpConnectionFailed": {"httpStatusCode": 401}}), "auth"
        )
        self.assertIsNone(
            failure_kind({"httpConnectionFailed": {"httpStatusCode": 403}})
        )
        self.assertEqual(
            failure_kind({"responseStreamDisconnected": {"httpStatusCode": 200}}),
            "transient",
        )

    def test_six_pool_clients_unique_threads_and_shared_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            pools = [Pool(Path(folder)) for _ in range(6)]
            try:
                for i, pool in enumerate(pools):
                    self.assertTrue(pool.claim(f"thread-{i}"))
                    pool.assign(f"account{i % 4}")
                self.assertEqual(sum(pools[0].loads().values()), 6)
                self.assertFalse(pools[1].claim("thread-0"))
                pools[0].mark("account0", "model", delay=500)
                self.assertEqual(
                    pools[5].deadline("account0", "model"),
                    pools[0].deadline("account0", "model"),
                )
                pools[0].close()
                self.assertTrue(pools[1].claim("thread-0"))
            finally:
                for pool in pools:
                    pool.close()

    def test_cache_contains_usage_only_and_is_shared(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b = Pool(Path(folder)), Pool(Path(folder))
            a.save("one", {"rateLimits": {"primary": {"usedPercent": 24}}})
            self.assertEqual(a.cached("one"), b.cached("one"))
            a.invalidate("one")
            self.assertIsNone(b.cached("one"))


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_startup_lock_prevents_shared_database_initialization_race(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            native = root / "fake-codex"
            native.write_text(
                "#!"
                + str(Path(sys.executable).resolve())
                + "\n"
                + """
import os,sys,time,json
from pathlib import Path
home=Path(os.environ['CODEX_HOME'])
try:
 fd=os.open(home/'native-migration-active',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
except FileExistsError:
 sys.exit(2)
time.sleep(.025)
os.close(fd); (home/'native-migration-active').unlink()
for raw in sys.stdin:
 value=json.loads(raw)
 if 'id' in value:
  print(json.dumps({'id':value['id'],'result':{'ok':True}}),flush=True)
"""
            )
            native.chmod(0o700)
            home = root / "home"
            backends = []

            async def start():
                rpc = await RPC.start(str(native), home)
                backends.append(rpc)
                await rpc.initialize()

            try:
                await asyncio.gather(*(start() for _ in range(6)))
                self.assertEqual(len(backends), 6)
                self.assertTrue(all(rpc.proc.returncode is None for rpc in backends))
            finally:
                await asyncio.gather(
                    *(rpc.close() for rpc in backends), return_exceptions=True
                )

    async def test_six_processes_serialize_only_credential_operation(self):
        # Actual OS locks across independent Python processes, not coroutine mocks.
        with tempfile.TemporaryDirectory() as folder:
            program = """
import asyncio,sys,json
from pathlib import Path
from pool import file_lock,Pool
async def main():
 root=Path(sys.argv[1]); pool=Pool(root)
 pool.assign('shared')
 assert pool.claim('thread-'+sys.argv[2])
 async with file_lock(root/'credential.lock'):
  p=root/'counter.json'; value=json.loads(p.read_text()) if p.exists() else {'active':0,'max':0,'finished':0}
  value['active']+=1; value['max']=max(value['max'],value['active']); p.write_text(json.dumps(value))
  await asyncio.sleep(.03)
  value=json.loads(p.read_text()); value['active']-=1; value['finished']+=1; p.write_text(json.dumps(value))
 await asyncio.sleep(.02)
 pool.close()
asyncio.run(main())
"""
            processes = [
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    program,
                    folder,
                    str(i),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                for i in range(6)
            ]
            for p in processes:
                out, err = await p.communicate()
                self.assertEqual(p.returncode, 0, err.decode())
            value = json.loads((Path(folder) / "counter.json").read_text())
            self.assertEqual(value, {"active": 0, "max": 1, "finished": 6})

    async def test_cancel_waiting_lock_does_not_leak_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lock"

            async def wait():
                async with file_lock(path):
                    pass

            async with file_lock(path):
                task = asyncio.create_task(wait())
                await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(wait(), 1)


class CompatibilityTests(unittest.TestCase):
    def fixture(self, root):
        methods = [
            "initialize",
            "account/read",
            "account/login/start",
            "account/rateLimits/read",
            "thread/start",
            "thread/resume",
            "turn/start",
            "turn/interrupt",
        ]
        (root / "v2").mkdir()
        (root / "ClientRequest.json").write_text(
            json.dumps(
                {"oneOf": [{"properties": {"method": {"enum": [m]}}} for m in methods]}
            )
        )
        (root / "v2/LoginAccountParams.json").write_text(
            json.dumps(
                {
                    "oneOf": [
                        {
                            "properties": {
                                "type": {"enum": ["chatgptAuthTokens"]},
                                "accessToken": {},
                                "chatgptAccountId": {},
                            }
                        }
                    ]
                }
            )
        )
        for name, fields in {
            "v2/TurnStartParams.json": ["threadId", "input"],
            "v2/TurnCompletedNotification.json": ["threadId", "turn"],
            "v2/GetAccountRateLimitsResponse.json": ["rateLimits"],
            "ChatgptAuthTokensRefreshParams.json": ["reason", "previousAccountId"],
        }.items():
            (root / name).write_text(
                json.dumps({"properties": dict.fromkeys(fields, {})})
            )

    def test_additive_protocol_changes_are_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            compatibility.validate_schema(root)
            file = root / "v2/LoginAccountParams.json"
            value = json.loads(file.read_text())
            value["oneOf"][0]["properties"]["newOptionalField"] = {"type": "string"}
            file.write_text(json.dumps(value))
            compatibility.validate_schema(root)

    def test_breaking_auth_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            file = root / "v2/LoginAccountParams.json"
            value = json.loads(file.read_text())
            value["oneOf"][0]["required"] = ["newRequiredField"]
            file.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                compatibility.validate_schema(root)


if __name__ == "__main__":
    unittest.main()
