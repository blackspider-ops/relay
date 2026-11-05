"""Native argv and working-directory regressions."""

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from launch import (
    backend_arguments,
    frontend_arguments,
    native_mode,
    translate,
    working_directory,
)
from router import dispatch, resume_target, run_pool


class ArgumentTests(unittest.TestCase):
    def test_values_that_look_like_flags_are_not_reinterpreted(self):
        argv = ["resume", "-c", "--yolo", "-i", "--sandbox", "-m", "--search"]
        self.assertEqual(translate(argv), (argv, {}))
        self.assertEqual(
            backend_arguments(argv), ["-c", "--yolo", "-c", 'model="--search"']
        )
        self.assertFalse(native_mode(["resume", "-i", "--oss"]))

    def test_attached_options_and_config_boundaries(self):
        argv, overrides = translate(
            [
                "resume",
                "-sworkspace-write",
                "-anever",
                "-mcustom",
                "--",
                "--yolo",
                "--search",
            ]
        )
        self.assertEqual(
            overrides, {"sandbox": "workspace-write", "approvalPolicy": "never"}
        )
        self.assertEqual(backend_arguments(argv), ["-c", 'model="custom"'])

    def test_approve_for_me_and_private_backend(self):
        argv, overrides = translate(["resume", "--approve-for-me", "--no-daemon"])
        self.assertEqual(argv, ["resume"])
        self.assertEqual(
            overrides,
            {
                "approvalPolicy": "on-request",
                "sandbox": "workspace-write",
                "approvalsReviewer": "auto_review",
            },
        )

    def test_native_modes_keep_command_and_option_values_distinct(self):
        for argv in (
            ["resume", "--oss"],
            ["resume", "--help"],
            ["--remote=ws://localhost:1", "resume"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(native_mode(argv))
        for argv in (
            ["resume", "--last"],
            ["exec", "--json", "hello"],
            ["-m", "custom", "review", "--uncommitted"],
            ["-c", "exec", "resume"],
            ["fork", "--", "--oss"],
            ["a prompt", "exec"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(native_mode(argv))

    def test_cwd_defaults_and_relative_explicit_options(self):
        with tempfile.TemporaryDirectory() as folder:
            cwd = str(Path(folder).resolve())
            self.assertEqual(frontend_arguments(["resume"], cwd), ["-C", cwd, "resume"])
            for option in (["-C", "child"], ["--cd=child"], ["-Cchild"]):
                argv = ["resume", *option, "--all", "-i", "picture.png"]
                target = str(Path(cwd) / "child")
                self.assertEqual(working_directory(argv, cwd), target)
                self.assertEqual(
                    frontend_arguments(argv, cwd),
                    ["-C", target, "resume", "--all", "-i", "picture.png"],
                )
            self.assertEqual(
                working_directory(["-c", "--cd=wrong", "--", "-Cwrong"], cwd), cwd
            )

    def test_uuid_in_an_option_is_not_a_resume_target(self):
        ident = "01a1aa71-5cb7-7bc0-8c23-bcb26f693cf7"
        self.assertIsNone(
            resume_target(["resume", "-m", ident], Path("/missing"), "/tmp")
        )
        self.assertEqual(
            resume_target(["resume", "-m", "custom", ident], Path("/missing"), "/tmp"),
            ident,
        )
        self.assertIsNone(
            resume_target(["resume", "named-session", ident], Path("/missing"), "/tmp")
        )


class LaunchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_effective_project_and_frontend_invocation_cwd_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            project = root / "project"
            project.mkdir()
            state = root / "state"
            state.mkdir()
            args = argparse.Namespace(
                codex_args=["--", "resume", "--cd", "project", "--all", "--yolo"],
                profile=None,
                first=None,
                name=None,
            )
            with (
                patch("os.getcwd", return_value=str(root)),
                patch(
                    "settings.load", return_value={"background_on_disconnect": True}
                ) as prefs,
                patch("service.start", return_value="test") as start,
                patch("service.attach", new=AsyncMock(return_value=0)) as attach,
            ):
                self.assertEqual(await run_pool(args, state, "/codex"), 0)
            self.assertEqual(start.call_args.kwargs["cwd"], str(project))
            self.assertEqual(
                start.call_args.kwargs["arguments"],
                ["-C", str(project), "resume", "--all", "--yolo"],
            )
            self.assertEqual(attach.call_args.kwargs["cwd"], str(root))
            self.assertTrue(
                all(call.args[1] == str(project) for call in prefs.call_args_list)
            )

    async def test_native_utilities_need_no_account_pool_or_protocol_adaptation(self):
        argv = ["--", "mcp", "list", "--", "literal value"]
        args = argparse.Namespace(command="run", codex_args=argv)
        process = type("Process", (), {"wait": AsyncMock(return_value=7)})()
        with (
            patch("launch.native_commands", return_value={"mcp"}),
            patch(
                "asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
            ) as spawn,
        ):
            self.assertEqual(await dispatch(args, Path("/unused"), "/codex"), 7)
        self.assertEqual(spawn.call_args.args, ("/codex", *argv[1:]))
