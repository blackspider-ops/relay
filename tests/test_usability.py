"""Offline checks for completion and account controls across idle boundaries."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import commands
import completion
from pool import Pool
from router import Controller, ControlRejected, RouterError
import service
from settings import atomic_json
from test_router import FakeAccount, FakeBackend, FakeClient


class SwitchBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_target_disabled_during_credentials_is_not_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = Pool(Path(folder))
            pool.assign("one")
            target = FakeAccount("two")
            original = target.credentials

            async def credentials(*args, **kwargs):
                pool.set_options("two", enabled=False)
                return await original(*args, **kwargs)

            target.credentials = credentials
            c = Controller([FakeAccount("one"), target], FakeBackend(), pool=pool)
            try:
                with self.assertRaisesRegex(ControlRejected, "disabled"):
                    await c.control("switch", {"account": "two"})
                self.assertEqual(c.index, 0)
                self.assertFalse(c.backend.calls)
            finally:
                pool.close()

    async def test_running_worker_returns_reason_without_replacing_its_work_status(
        self,
    ):
        from test_services import ServiceTests, wait_for

        case = ServiceTests("test_background_work_completes_without_frontend")
        await case.asyncSetUp()
        pool = Pool(case.state)
        try:
            await wait_for(lambda: json.loads(case.job.read_text()).get("resume"))
            command = pool.command(case.ident, "switch", {"account": "not-enrolled"})
            with self.assertRaisesRegex(RouterError, "no longer enrolled"):
                await service.wait_control(pool, command, timeout=5)
            self.assertNotEqual(pool.detail()[0].get("state"), "control_failed")
        finally:
            pool.close()
            await case.asyncTearDown()

    async def test_work_arriving_during_allowance_or_credentials_prevents_switch(self):
        for operation in ("usage", "credentials"):
            for field, marker in (
                ("active_threads", "thread"),
                ("active_items", "tool"),
                ("server_requests", 7),
            ):
                with (
                    self.subTest(operation=operation, field=field),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    pool = Pool(Path(folder))
                    pool.assign("one")
                    account = FakeAccount("two")
                    original = getattr(account, operation)
                    entered, release = asyncio.Event(), asyncio.Event()

                    async def delayed(*args, **kwargs):
                        entered.set()
                        await release.wait()
                        return await original(*args, **kwargs)

                    setattr(account, operation, delayed)
                    backend = FakeBackend()
                    c = Controller([FakeAccount("one"), account], backend, pool=pool)
                    task = asyncio.create_task(c.control("switch", {"account": "two"}))
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        getattr(c, field).add(marker)
                        release.set()
                        with self.assertRaisesRegex(ControlRejected, "busy"):
                            await asyncio.wait_for(task, 1)
                        self.assertEqual(c.account.label, "one")
                        self.assertFalse(
                            any(m == "account/login/start" for m, _ in backend.calls)
                        )
                        with pool.db() as db:
                            self.assertEqual(
                                db.execute(
                                    "SELECT count(*) FROM reservations"
                                ).fetchone()[0],
                                0,
                            )
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        pool.close()

    async def test_switch_rechecks_after_waiting_for_login_lock(self):
        c = Controller([FakeAccount("one"), FakeAccount("two")], FakeBackend())
        await c.login_lock.acquire()
        task = asyncio.create_task(c.control("switch", {"account": "two"}))
        await asyncio.sleep(0)
        c.active_threads.add("new-work")
        c.login_lock.release()
        with self.assertRaises(ControlRejected):
            await asyncio.wait_for(task, 1)
        self.assertEqual(c.index, 0)

    async def test_automatic_continuation_waits_for_account_transition(self):
        c = Controller([FakeAccount("one")], FakeBackend())
        c.client = FakeClient()
        await c.login_lock.acquire()
        task = asyncio.create_task(c.continue_thread("thread", 0))
        await asyncio.sleep(0)
        self.assertFalse(any(m == "turn/start" for m, _ in c.backend.calls))
        c.login_lock.release()
        await asyncio.wait_for(task, 1)
        self.assertEqual(sum(m == "turn/start" for m, _ in c.backend.calls), 1)

    async def test_control_rejection_is_specific_but_unknown_details_are_not_echoed(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            pool = Pool(Path(folder))
            for reason, expected in (
                ("busy", "Session is busy"),
                ("PRIVATE_TEST_SECRET", "Session rejected"),
                (None, "Session rejected"),
            ):
                command = pool.command("worker", "switch", {"account": "two"})
                pool.ack(command, "failed", reason=reason)
                with self.assertRaisesRegex(RouterError, expected) as error:
                    await service.wait_control(pool, command)
                self.assertNotIn("PRIVATE_TEST_SECRET", str(error.exception))
            pool.close()


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        home = self.state / "accounts" / "personal"
        home.mkdir(parents=True)
        atomic_json(home / "enrolled", {})
        atomic_json(home / "identity.json", {"email": "person@example.invalid"})
        atomic_json(self.state / "workspaces.json", {"daily work": ["saved"]})
        atomic_json(self.state / "settings.json", {"profiles": {"quiet": {}}})
        atomic_json(
            self.state / "services" / "saved.job.json",
            {"id": "saved", "first": "personal", "state": "needs_attention"},
        )
        self.pool = Pool(self.state)
        self.pool.session = "live-test"
        self.pool.assign("personal")

    def tearDown(self):
        self.pool.close()
        self.tmp.cleanup()

    def complete(self, *args):
        with (
            patch(
                "router.read_keychain",
                side_effect=AssertionError("No credential reads"),
            ),
            patch("router.codex_path", side_effect=AssertionError("No native process")),
        ):
            return completion.candidates(args, self.state)

    def test_parser_commands_options_and_dynamic_values(self):
        self.assertIn("completion", self.complete(""))
        self.assertFalse(any(x.startswith("_") for x in self.complete("")))
        self.assertIn("reauth", self.complete("account", "re"))
        self.assertEqual(self.complete("completion", "z"), ["zsh"])
        self.assertEqual(
            self.complete("switch", "--account", "pe"),
            ["person@example.invalid", "personal"],
        )
        self.assertEqual(
            self.complete("switch", "--account=pers"),
            ["--account=person@example.invalid", "--account=personal"],
        )
        self.assertEqual(self.complete("run", "--profile", ""), ["quiet"])
        self.assertEqual(self.complete("workspace", "restore", ""), ["daily work"])
        self.assertEqual(self.complete("pause", "live"), ["live-test"])
        self.assertIn("saved", self.complete("restart", ""))
        self.assertEqual(
            self.complete("config", "notifications", ""), ["false", "true"]
        )

    def test_native_boundaries_consume_wrapper_values(self):
        for args, skip in (
            (["run", "--", "resume", "--la"], 2),
            (["run", "--name", "--", "--", "resume", ""], 4),
            (["run", "resume", ""], 1),
            (["attach", "saved", "--", ""], 3),
            (["explain", "--", ""], 2),
        ):
            self.assertEqual(self.complete(*args), [f"@codex:{skip}"])
        self.assertIn("--require-recovery", self.complete("run", "--"))

    def test_stopped_sessions_keep_account_identity_and_correct_liveness(self):
        value = commands.snapshot(self.state)
        saved = next(s for s in value["sessions"] if s["id"] == "saved")
        self.assertFalse(saved["running"])
        self.assertEqual(saved["account_name"], "person@example.invalid")
        self.assertTrue(
            next(s for s in value["sessions"] if s["id"] == "live-test")["running"]
        )

    def test_real_shell_adapters_preserve_spaces_and_delegate_native_arguments(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        relay = bin_dir / "relay"
        relay.write_text(
            "#!"
            + sys.executable
            + "\nimport sys\nfrom pathlib import Path\nfrom completion import candidates\nprint('\\n'.join(candidates(sys.argv[3:], Path("
            + repr(str(self.state))
            + "))))\n"
        )
        relay.chmod(0o700)
        env = {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "python"),
        }
        for shell in ("bash", "zsh"):
            script = self.root / ("completion." + shell)
            script.write_text(completion.script(shell))
            if shell == "bash":
                command = """source "$1"
COMP_WORDS=(relay workspace restore ""); COMP_CWORD=3
_relay; printf '%s\n' "${COMPREPLY[@]}"
_codex() { COMPREPLY=("native:${COMP_WORDS[*]}:${COMP_CWORD}"); }
COMP_WORDS=(relay run -- resume --la); COMP_CWORD=4
_relay; printf '%s\n' "${COMPREPLY[@]}"
"""
            else:
                command = """autoload -Uz compinit; compinit -D
source "$1"
_files() { return 1; }
compadd() { local -a result; result=("${(@P)2}"); printf '%s\n' "${result[@]}"; }
words=(relay workspace restore ""); CURRENT=4; _relay
_codex() { print -r -- "native:${words[*]}:${CURRENT}"; }
words=(relay run -- resume --la); CURRENT=5; _relay
"""
            result = subprocess.run(
                [
                    "/bin/" + shell,
                    "-f" if shell == "zsh" else "--noprofile",
                    "-c",
                    command,
                    "test",
                    str(script),
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "daily work",
                    "native:codex resume --la:2"
                    if shell == "bash"
                    else "native:codex resume --la:3",
                ],
            )


if __name__ == "__main__":
    unittest.main()
