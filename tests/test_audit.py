"""Regression cases reproduced by the audit. No real accounts or model calls."""

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from router import Controller, RemoteError, parse_arguments, resolve_resume_target
from test_router import FakeAccount, FakeBackend, FakeClient, completed
from test_pool import Clock
from pool import Pool, process_birth
from settings import atomic_json
import commands
import service


class RecoveryRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_late_failure_cannot_bypass_review_hold(self):
        with tempfile.TemporaryDirectory() as folder:
            clock = Clock()
            pool = Pool(Path(folder), clock=clock)
            backend = FakeBackend()
            c = Controller(
                [FakeAccount("one")],
                backend,
                pool=pool,
                clock=clock,
                sleep=clock.sleep,
                report=lambda _: None,
            )
            c.client = FakeClient()
            c.last_turn["thread-1"] = {"threadId": "thread-1"}
            c.hold_for_review("thread-1")
            await c.event(completed(code="policyCapacityUnavailable"))
            await c.recovery
            self.assertFalse(any(method == "turn/start" for method, _ in backend.calls))
            self.assertTrue(pool.intent("thread-1")["review_required"])
            await c.control("retry", {"thread": "thread-1"})
            await c.recovery
            self.assertTrue(any(method == "turn/start" for method, _ in backend.calls))
            pool.close()

    async def test_explicit_retry_clears_a_review_hold_during_an_existing_timer(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = Pool(Path(folder))
            backend = FakeBackend()
            c = Controller(
                [FakeAccount("one")], backend, pool=pool, report=lambda _: None
            )
            c.client = FakeClient()
            c.last_turn["thread-1"] = {"threadId": "thread-1"}
            c.recovery = asyncio.create_task(c.recover("thread-1", 0, "capacity"))
            try:
                while not c.episodes:
                    await asyncio.sleep(0)
                c.hold_for_review("thread-1")
                await c.control("retry", {"thread": "thread-1"})
                await asyncio.wait_for(c.recovery, 1)
                self.assertFalse(pool.intent("thread-1")["review_required"])
                self.assertEqual(
                    sum(method == "turn/start" for method, _ in backend.calls), 1
                )
            finally:
                c.recovery.cancel()
                await asyncio.gather(c.recovery, return_exceptions=True)
                pool.close()

    async def test_picker_permission_control_applies_before_thread_selection(self):
        c = Controller([FakeAccount("one")], FakeBackend(), report=lambda _: None)
        c.launch_overrides = {
            "sandbox": "danger-full-access",
            "approvalPolicy": "never",
        }
        await c.control(
            "permissions", {"sandbox": "read-only", "approvalPolicy": "on-request"}
        )
        self.assertEqual(
            c.launch_overrides, {"sandbox": "read-only", "approvalPolicy": "on-request"}
        )
        c.client = FakeClient()
        await c.forward({"id": 1, "method": "thread/start", "params": {}}, {})
        self.assertEqual(c.backend.calls[-1][1]["sandbox"], "read-only")

    async def test_more_than_one_thousand_overloads_do_not_grow_call_stack(self):
        clock = Clock()
        backend = FakeBackend()
        rejections = 1100
        original = backend.call

        async def overloaded(method, params=None, **kw):
            nonlocal rejections
            if method == "turn/start" and rejections:
                rejections -= 1
                raise RemoteError({"code": -32001})
            return await original(method, params, **kw)

        backend.call = overloaded
        c = Controller(
            [FakeAccount("one"), FakeAccount("two")],
            backend,
            clock=clock,
            sleep=clock.sleep,
            report=lambda _: None,
        )
        c.client = FakeClient()
        c.last_turn["thread-1"] = {"threadId": "thread-1"}
        await c.recover("thread-1", 0, "transient")
        self.assertEqual(rejections, 0)
        self.assertTrue(any(method == "turn/start" for method, _ in backend.calls))
        self.assertFalse(c.paused_uncertain)

    async def test_resume_lookup_uses_native_filtering_and_names(self):
        class Backend:
            def __init__(self):
                self.calls = []

            async def call(self, method, params):
                self.calls.append((method, dict(params)))
                return {
                    "data": [{"id": "interactive", "name": "named", "cwd": "/project"}]
                }

        backend = Backend()
        selected = await resolve_resume_target(
            ["resume", "--last"], Path("/home"), "/project", backend=backend
        )
        self.assertEqual(selected, "interactive")
        self.assertNotIn("sourceKinds", backend.calls[-1][1])
        self.assertEqual(backend.calls[-1][1]["cwd"], "/project")
        selected = await resolve_resume_target(
            ["resume", "named", "--all", "--include-non-interactive"],
            Path("/home"),
            "/project",
            backend=backend,
        )
        self.assertEqual(selected, "interactive")
        self.assertIn("exec", backend.calls[-1][1]["sourceKinds"])
        self.assertNotIn("cwd", backend.calls[-1][1])

    async def test_watch_ignores_heartbeats_and_redraws_only_meaningful_changes(self):
        args = type(
            "Args", (), {"watch": True, "json": True, "all": False, "interval": 30}
        )()
        count = 0
        values = [
            {"at": 1, "sessions": [{"id": "a", "seen": 1, "state": "idle"}]},
            {"at": 2, "sessions": [{"id": "a", "seen": 2, "state": "idle"}]},
            {"at": 3, "sessions": [{"id": "a", "seen": 3, "state": "working"}]},
        ]

        class Watch:
            def __init__(self, state):
                pass

            async def changed(self, timeout):
                nonlocal count
                count += 1
                if count == 3:
                    raise asyncio.CancelledError()

            def close(self):
                pass

        output = io.StringIO()
        with (
            patch("watch.LocalChanges", Watch),
            patch("commands.snapshot", side_effect=values),
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await commands.status(args, Path("/unused"))
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    async def test_control_wait_reports_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            pool = Pool(state)
            pool.session = "worker"
            pool.assign("one")
            args = parse_arguments(["pause", "worker", "--wait"])

            async def reject(pool, ident, timeout):
                raise ValueError("rejected")

            with patch("service.wait_control", reject):
                with self.assertRaisesRegex(ValueError, "rejected"):
                    await commands.control(args, state)
            pool.close()


class WorkerRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_services import ServiceTests

        self.case = ServiceTests("test_background_work_completes_without_frontend")
        await self.case.asyncSetUp()

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_worker_restores_reviewer_and_workspace_roots(self):
        from test_services import wait_for
        from router import RPC

        c = self.case
        await wait_for(lambda: json.loads(c.job.read_text()).get("resume"))
        await wait_for(
            lambda: any(
                t["turns"] and t["turns"][-1]["status"] == "completed"
                for t in json.loads((c.home / "fake-history.json").read_text())[
                    "threads"
                ].values()
            )
        )
        thread = json.loads(c.job.read_text())["resume"]
        c.task.cancel()
        await asyncio.gather(c.task, return_exceptions=True)
        pool = Pool(c.state)
        pool.session = c.ident
        pool.intent(
            thread,
            {
                "role": "foreground",
                "accepted": True,
                "state": "completed",
                "settings": {
                    "approvalsReviewer": "auto_review",
                    "runtimeWorkspaceRoots": [str(c.home), "/private/tmp/audit-extra"],
                },
            },
        )
        observed = []
        original = RPC.call

        async def capture(rpc, method, params=None, **kw):
            if method == "thread/resume":
                observed.append(params)
            return await original(rpc, method, params, **kw)

        with patch.object(RPC, "call", capture):
            c.task = asyncio.create_task(service.serve(c.state, c.ident, c.binary))
            await wait_for(c.meta.exists)
        self.assertEqual(observed[0]["approvalsReviewer"], "auto_review")
        self.assertIn("/private/tmp/audit-extra", observed[0]["runtimeWorkspaceRoots"])
        pool.close()

    async def test_project_selection_updates_restart_and_attach_metadata(self):
        from websockets.asyncio.client import connect

        c = self.case
        other = c.home / "other"
        other.mkdir()
        value = json.loads(c.meta.read_text())
        async with connect(
            value["url"],
            additional_headers={"Authorization": "Bearer " + value["token"]},
            proxy=None,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": "select",
                        "method": "thread/start",
                        "params": {"cwd": str(other)},
                    }
                )
            )
            while json.loads(await ws.recv()).get("id") != "select":
                pass
        self.assertEqual(json.loads(c.meta.read_text())["cwd"], str(other))
        self.assertEqual(json.loads(c.job.read_text())["cwd"], str(other))


class AttachmentRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_new_worker_reconnects_without_replaying_original_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            root = service.folder(state)
            job = root / "task.job.json"
            meta = root / "task.connection.json"
            atomic_json(job, {"binary": "/codex", "state": "running"})
            old = {
                "pid": os.getpid(),
                "birth": process_birth(os.getpid()),
                "url": "ws://127.0.0.1:1",
                "token": "FAKE",
                "home": folder,
                "cwd": folder,
                "thread": "saved",
            }
            atomic_json(meta, old)

            class Process:
                def __init__(self, done=False):
                    self.returncode = 0 if done else None
                    self.finished = asyncio.Event()

                async def wait(self):
                    if self.returncode is None:
                        await self.finished.wait()
                    return self.returncode

                def terminate(self):
                    self.returncode = 0
                    self.finished.set()

                def kill(self):
                    self.terminate()

            spawned = []

            async def spawn(*argv, **kwargs):
                spawned.append(argv)
                if len(spawned) == 1:
                    atomic_json(
                        meta,
                        {
                            **old,
                            "pid": os.getppid(),
                            "birth": process_birth(os.getppid()),
                            "url": "ws://127.0.0.1:2",
                            "token": "NEW_FAKE",
                        },
                    )
                return Process(done=len(spawned) > 1)

            with patch("asyncio.create_subprocess_exec", spawn):
                self.assertEqual(
                    await service.attach(state, "task", "/codex", ["original prompt"]),
                    0,
                )
            self.assertEqual(len(spawned), 2)
            self.assertIn("saved", spawned[1])
            self.assertNotIn("original prompt", spawned[1])

    def test_takeover_can_follow_session_but_not_native_separator(self):
        self.assertTrue(parse_arguments(["attach", "task", "--takeover"]).takeover)
        self.assertFalse(
            parse_arguments(["attach", "task", "--", "--takeover"]).takeover
        )
