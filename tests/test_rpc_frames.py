"""Large native catalogs and malformed frames must not silently kill RPC."""

import asyncio
import sys
import unittest

from router import BackendDisconnected, Controller, RPC, RPC_MESSAGE_LIMIT
from test_router import FakeAccount, FakeBackend, FakeClient


class FrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_native_reply_and_notification_leave_connection_usable(self):
        program = """
import json,sys
for raw in sys.stdin:
    d=json.loads(raw)
    if d['method']=='large':
        # Slightly exceed the old 32 MiB cap, as native plugin/list does.
        asset='x'*(34*1024*1024)
        print(json.dumps({'method':'catalog/updated','params':{'asset':asset}}),flush=True)
        reply={'id':d['id'],'result':{'asset':asset}}
    else:
        reply={'id':d['id'],'result':{'ready':True}}
    print(json.dumps(reply),flush=True)
"""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            program,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=RPC_MESSAGE_LIMIT,
        )
        notifications = []

        async def handler(message):
            notifications.append((message["method"], len(message["params"]["asset"])))

        rpc = RPC(proc, handler)
        try:
            result = await rpc.call("large", timeout=15)
            self.assertEqual(len(result["asset"]), 34 * 1024 * 1024)
            del result
            self.assertEqual(await rpc.call("next"), {"ready": True})
            self.assertEqual(notifications, [("catalog/updated", 34 * 1024 * 1024)])
            self.assertFalse(rpc.disconnected())
        finally:
            await rpc.close()

    async def assert_invalid_frame(self, frame, reason, limit=1024):
        # The child stays alive: reader failure, not process exit, is the fault.
        program = """
import sys,time
sys.stdin.readline()
sys.stdout.write(sys.argv[1]);sys.stdout.flush()
time.sleep(60)
"""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            program,
            frame,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=limit,
        )
        rpc = RPC(proc)
        try:
            # Both already queued and subsequent calls receive safe guidance.
            results = await asyncio.gather(
                rpc.call("first", timeout=2),
                rpc.call("second", timeout=2),
                return_exceptions=True,
            )
            for error in results:
                self.assertIsInstance(error, BackendDisconnected)
                self.assertEqual(error.reason, reason)
                self.assertNotIn("PRIVATE", str(error))
            self.assertIsNone(proc.returncode)
            self.assertTrue(rpc.disconnected())
            with self.assertRaises(BackendDisconnected) as caught:
                await rpc.call("later")
            self.assertEqual(caught.exception.reason, reason)
            self.assertFalse(rpc.pending)
        finally:
            await rpc.close()

    async def test_oversized_frame_is_identified_without_logging_contents(self):
        await self.assert_invalid_frame("PRIVATE" * 300 + "\n", "response_too_large")

    async def test_invalid_json_or_rpc_shape_is_identified(self):
        for frame in ("PRIVATE\n", "[]\n", '{"id":{}}\n', '"PRIVATE"\n'):
            with self.subTest(frame=frame):
                await self.assert_invalid_frame(frame, "invalid_response")

    async def test_turn_failure_reports_transport_reason_without_resubmitting(self):
        class BrokenBackend(FakeBackend):
            async def call(self, method, params=None, **kwargs):
                self.calls.append((method, params))
                raise BackendDisconnected("response_too_large")

        backend = BrokenBackend()
        controller = Controller([FakeAccount("one")], backend)
        controller.client = FakeClient()
        controller.turn_ids["saved"] = "baseline"

        async def reconcile_only(*args):
            pass

        controller.uncertain = reconcile_only
        await controller.forward(
            {
                "id": "submit",
                "method": "turn/start",
                "params": {"threadId": "saved", "input": []},
            },
            {},
        )
        if controller.recovery:
            await controller.recovery
        error = controller.client.messages[-1]["error"]
        self.assertIn("256 MiB", error["message"])
        self.assertIn("No automatic retry", error["message"])
        self.assertEqual([m for m, _ in backend.calls], ["turn/start"])
        self.assertEqual(error["code"], -32000)

    async def test_replacement_releases_old_writer_before_resume_and_preserves_review(
        self,
    ):
        for active in (False, True):
            with self.subTest(active=active):

                class Old:
                    closed = False

                    def disconnected(self):
                        return True  # Reader failed, but native still owns its thread.

                    async def close(self):
                        self.closed = True

                old = Old()

                class Replacement(FakeBackend):
                    async def initialize(self):
                        return {}

                    async def call(self, method, params=None, **kwargs):
                        if method == "thread/resume":
                            if not old.closed:
                                raise AssertionError(
                                    "old native writer still owns thread"
                                )
                        if method == "thread/turns/list":
                            return {"data": [{"id": "finished", "status": "completed"}]}
                        if method == "thread/read":
                            return {"thread": {"status": {"type": "idle"}}}
                        return await super().call(method, params, **kwargs)

                replacement = Replacement()
                controller = Controller([FakeAccount("one")], old)
                controller.client = FakeClient()
                controller.last_turn["saved"] = {"threadId": "saved"}
                if active:
                    controller.active_threads.add("saved")

                async def factory():
                    return replacement

                controller.backend_factory = factory
                await controller.restart_backend()
                self.assertTrue(old.closed)
                self.assertIs(controller.backend, replacement)
                self.assertTrue(any(m == "thread/resume" for m, _ in replacement.calls))
                self.assertEqual("saved" in controller.paused_uncertain, active)
                self.assertFalse(any(m == "turn/start" for m, _ in replacement.calls))


if __name__ == "__main__":
    unittest.main()
