"""Local management and native-option behavior, with isolated state."""

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from launch import backend_arguments, native_mode, prepare_worktree, translate
from pool import Pool
from router import parse_arguments
from settings import atomic_json
import commands
import desktop
import execution
import tools


class ManagementTests(unittest.TestCase):
    def test_profile_layer_and_explicit_config_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "work.config.toml").write_text(
                'model="profile-model"\n[features]\nexample=true\n'
            )
            with patch.dict(os.environ, {"CODEX_HOME": folder}):
                args = backend_arguments(["-pwork", "-c", 'model="explicit-model"'])
                self.assertEqual(args[-2:], ["-c", 'model="explicit-model"'])
                self.assertIn('model="profile-model"', args)
                self.assertIn("features.example=true", args)
                self.assertEqual(
                    translate(["resume", "-pwork", "--add-dir", "/tmp"])[0], ["resume"]
                )
                with self.assertRaises(ValueError):
                    backend_arguments(["-p", "../outside"])
                self.assertFalse(native_mode(["resume", "-pwork", "--add-dir=/tmp"]))

    def test_background_cd_resolves_against_supplied_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "child").mkdir()
            cwd, settings, backend = __import__("service").headless_options(
                ["-Cchild", "-mtest", "-c", "features.x=true"], str(root)
            )
            self.assertEqual(cwd, str(root / "child"))
            self.assertEqual(settings["model"], "test")
            self.assertEqual(backend[-2:], ["-c", "features.x=true"])

    def test_real_git_worktree_is_separate_and_resume_combination_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            repo = root / "repo"
            state = root / "state"
            repo.mkdir()
            state.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "test",
                ],
                check=True,
            )
            args, cwd = prepare_worktree(["--worktree", "hello"], state, str(repo))
            self.assertNotEqual(cwd, str(repo))
            self.assertTrue((Path(cwd) / ".git").exists())
            self.assertNotIn("--worktree", args)
            with self.assertRaises(ValueError):
                prepare_worktree(["resume", "--worktree"], state, str(repo))

    def test_both_displays_receive_the_same_fractional_allowance(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            home = state / "accounts" / "one"
            home.mkdir(parents=True)
            (home / "enrolled").write_text("{}")
            pool = Pool(state)
            pool.save(
                "one",
                {
                    "rateLimits": {
                        "primary": {"usedPercent": 70.2},
                        "secondary": {"usedPercent": 41.5},
                    }
                },
            )
            value = commands.snapshot(state)
            account = value["accounts"][0]
            self.assertEqual(account["short_remaining"], "29.8%")
            self.assertEqual(account["weekly_remaining"], "58.5%")
            text = commands.render(value)
            self.assertIn(account["short_remaining"], text)
            self.assertIn(account["weekly_remaining"], text)
            pool.close()

    def test_account_emails_are_displayed_and_resolve_without_reading_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            home = state / "accounts" / "account1"
            home.mkdir(parents=True)
            atomic_json(home / "enrolled", {"identity": "synthetic"})
            atomic_json(home / "identity.json", {"email": "person@example.invalid"})
            with patch(
                "router.read_keychain",
                side_effect=AssertionError("Status must not read Keychain"),
            ):
                result = commands.snapshot(state)
                self.assertEqual(
                    result["accounts"][0]["name"], "person@example.invalid"
                )
                self.assertEqual(
                    commands.resolve_label(state, "person@example.invalid"), "account1"
                )

    def test_redacted_diagnostics_omit_paths_names_and_prompts(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            pool = Pool(state)
            pool.assign("private-label")
            pool.touch(
                cwd="/secret/project",
                name="secret-task",
                state="working",
                prompt="PRIVATE_PROMPT",
            )
            pool.record("working", project="/secret/project", email="PRIVATE_EMAIL")
            target = state / "diagnostics.json"
            tools.diagnostics(state, target)
            output = target.read_text()
            for secret in [
                "/secret/project",
                "private-label",
                "secret-task",
                "PRIVATE_PROMPT",
                "PRIVATE_EMAIL",
                folder,
            ]:
                self.assertNotIn(secret, output)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            pool.close()

    def test_cleanup_dry_run_retains_workspace_and_runtime_references(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            old = state / "native-runtimes" / "old"
            old.mkdir(parents=True)
            (old / "binary").write_text("test")
            unused = state / "relay-runtimes" / "unused"
            unused.mkdir(parents=True)
            job = state / "services" / "saved.job.json"
            atomic_json(job, {"state": "stopped", "binary": str(old / "binary")})
            atomic_json(state / "workspaces.json", {"daily": ["saved"]})
            before = time.time() - 90 * 86400
            for p in [old, unused, job]:
                os.utime(p, (before, before))
            value = tools.cleanup(state, days=30, jobs=True)
            self.assertTrue(value["dry_run"])
            self.assertEqual(value["paths"], [str(unused)])
            self.assertTrue(unused.exists())
            self.assertTrue(job.exists())

    def test_launch_agent_uses_stable_app_and_has_no_auth_token(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state"
            with patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], 0)
            ):
                path = desktop.login_item(state, enabled=True, home=folder)
                data = plistlib.loads(path.read_bytes())
                self.assertIn("RelayMenu", data["ProgramArguments"][0])
                self.assertTrue(data["RunAtLoad"])
                self.assertNotIn("token", path.read_text().lower())
                desktop.login_item(state, enabled=False, home=folder)
                self.assertFalse(path.exists())

    @unittest.skipUnless(sys.platform == "darwin", "Native app bundle test")
    def test_native_menu_bundle_signs_and_symbols_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            app = desktop.menubar(
                Path(folder), launcher="/usr/bin/false", build_only=True
            )
            verify = subprocess.run(
                ["/usr/bin/codesign", "--verify", "--strict", str(app)],
                capture_output=True,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr.decode())
            result = subprocess.run(
                [str(app / "Contents/MacOS/RelayMenu"), "--self-check"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_secret_profile_fields_do_not_enter_process_arguments_or_pool_settings(
        self,
    ):
        from launch import profile_config
        from router import Controller
        from test_router import FakeAccount, FakeBackend

        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "private.config.toml").write_text(
                'model="normal"\n[mcp_servers.example.http_headers]\nAuthorization="PRIVATE_TEST_SECRET"\n'
            )
            with patch.dict(os.environ, {"CODEX_HOME": folder}):
                arguments = backend_arguments(["-p", "private"])
                self.assertNotIn("PRIVATE_TEST_SECRET", " ".join(arguments))
                overrides = profile_config(["-p", "private"])
                self.assertIn("PRIVATE_TEST_SECRET", json.dumps(overrides))
                c = Controller(
                    [FakeAccount("one")], FakeBackend(), report=lambda _: None
                )
                c.merge_settings("thread", {"config": overrides})
                self.assertNotIn("PRIVATE_TEST_SECRET", json.dumps(c.thread_settings))

    def test_help_command_and_combined_run_help(self):
        import router

        for args in [
            [],
            ["help"],
            ["help", "workspace"],
            ["help", "run"],
            ["run", "--help"],
        ]:
            with (
                self.subTest(args=args),
                patch.object(sys, "argv", ["relay", *args]),
                patch("router.codex_path", return_value="/codex"),
                patch("subprocess.call", return_value=0),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                if args in (["help", "run"], ["run", "--help"]):
                    self.assertEqual(router.main(), 0)
                    self.assertIn("Relay launch options", out.getvalue())
                else:
                    with self.assertRaises(SystemExit) as end:
                        router.main()
                    self.assertEqual(end.exception.code, 0)
                    self.assertNotIn("router.py", out.getvalue())


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_exec_json_and_last_message_over_real_jsonl(self):
        from test_services import fake_executable
        from test_router import FakeAccount

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            home.mkdir()
            state = root / "state"
            state.mkdir()
            for label in ("one", "two"):
                account = state / "accounts" / label
                account.mkdir(parents=True)
                (account / "enrolled").write_text("{}")
            binary = fake_executable(root)
            output_file = root / "answer.txt"
            args = parse_arguments(
                [
                    "run",
                    "--",
                    "exec",
                    "--skip-git-repo-check",
                    "--json",
                    "-o",
                    str(output_file),
                    "test only",
                ]
            )

            class Account(FakeAccount):
                def __init__(self, label, *args):
                    super().__init__(label)

            output = io.StringIO()
            with (
                patch.dict(os.environ, {"CODEX_HOME": str(home)}),
                patch("broker.ClientAccount", Account),
                patch("sys.stdin", io.StringIO("")),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(
                    await asyncio.wait_for(execution.run(args, state, binary), 8), 0
                )
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(events[0]["type"], "thread.started")
            self.assertEqual(events[-1]["type"], "turn.completed")
            self.assertEqual(output_file.read_text(), "FAKE_OK\n")
            history = json.loads((home / "fake-history.json").read_text())
            self.assertEqual(
                sum(call["method"] == "turn/start" for call in history["calls"]), 1
            )

    async def test_noninteractive_approval_is_not_automatically_accepted(self):
        output = execution.Output(True)
        with contextlib.redirect_stderr(io.StringIO()):
            await output.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "item/commandExecution/requestApproval",
                        "params": {},
                    }
                )
            )
        self.assertTrue(output.done.is_set())
        self.assertEqual(output.status, 1)

    async def test_exec_json_includes_tool_records(self):
        output = execution.Output(True)
        output.thread = "parent"
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            for kind in ("mcpToolCall", "webSearch", "collabAgentToolCall"):
                await output.send(
                    json.dumps(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": "parent",
                                "item": {"id": kind, "type": kind},
                            },
                        }
                    )
                )
        self.assertEqual(
            [
                json.loads(line)["item"]["type"]
                for line in stream.getvalue().splitlines()
            ],
            ["mcp_tool_call", "web_search", "collab_tool_call"],
        )

    async def test_only_foreground_thread_can_finish_exec(self):
        output = execution.Output(True)
        output.thread = "parent"
        await output.send(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "child", "turn": {"status": "completed"}},
                }
            )
        )
        self.assertFalse(output.done.is_set())

    def test_native_exec_subcommands_and_prompt_separator(self):
        value = execution.options(
            ["exec", "resume", "saved", "--json", "--", "--yolo"], "/tmp"
        )
        self.assertEqual(value["mode"], "resume")
        self.assertEqual(value["target"], "saved")
        self.assertEqual(value["prompt"], "--yolo")
        self.assertNotIn("--yolo", value["flags"])
        self.assertTrue(native_mode(["exec", "help"]))
