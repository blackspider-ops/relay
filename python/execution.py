"""Noninteractive Codex tasks using Relay's existing account/recovery controller."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from launch import (
    arguments_only,
    tokens,
    translate,
    backend_arguments,
    working_directory,
    additional_roots,
    prepare_worktree,
)


def command(arguments):
    for name, value, raw in tokens(arguments_only(arguments)):
        if name is None and len(raw) == 1:
            return value if value in {"exec", "e", "review"} else None
    return None


def options(arguments, cwd):
    parsed = list(tokens(arguments_only(arguments)))
    positional = []
    values, flags = {}, set()
    allowed = {
        "--config",
        "--profile",
        "--enable",
        "--disable",
        "--model",
        "--image",
        "--cd",
        "--sandbox",
        "--ask-for-approval",
        "--permission-profile",
        "--add-dir",
        "--thread-source",
        "--cyber-access-program",
        "--output-schema",
        "--color",
        "--output-last-message",
        "--base",
        "--commit",
        "--title",
        "--search",
        "--strict-config",
        "--approve-for-me",
        "--yolo",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--ephemeral",
        "--json",
        "--last",
        "--all",
        "--include-non-interactive",
        "--uncommitted",
        "--no-daemon",
        "--no-alt-screen",
    }
    for name, value, raw in parsed:
        if name is None:
            positional.extend(raw[1:] if raw[:1] == ["--"] else raw)
        else:
            if name not in allowed:
                raise ValueError("This exec option needs native Codex: " + name)
            flags.add(name)
            if name in {"--image", "--add-dir"}:
                values.setdefault(name, []).append(value)
            else:
                values[name] = value
    top = positional.pop(0)
    mode = "review" if top == "review" else "start"
    if top != "review" and positional[:1] in (["resume"], ["fork"], ["review"]):
        mode = positional.pop(0)
    target = None
    if mode in {"resume", "fork"} and "--last" not in flags:
        if not positional:
            raise ValueError("Choose a session ID/name or use --last")
        target = positional.pop(0)
    if len(positional) > 1:
        raise ValueError("Provide one quoted prompt, as with native Codex")
    prompt = positional[0] if positional else None
    return {
        "mode": mode,
        "target": target,
        "prompt": prompt,
        "values": values,
        "flags": flags,
        "cwd": working_directory(arguments, cwd),
    }


class Output:
    def __init__(self, json_output=False):
        self.json_output = json_output
        self.done = asyncio.Event()
        self.status = 0
        self.last_message = ""
        self.controller = None
        self.usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        self.thread = None

    def write(self, value):
        if self.json_output:
            print(json.dumps(value), flush=True)

    async def send(self, raw):
        value = json.loads(raw)
        method, params = value.get("method"), value.get("params", {})
        if value.get("id") == "exec-start" and value.get("error"):
            if value["error"].get("code") != -32001:
                self.status = 1
                self.write(
                    {
                        "type": "error",
                        "message": "Codex rejected the task. Check the saved conversation and options.",
                    }
                )
                self.done.set()
            return
        if self.thread and params.get("threadId") not in {None, self.thread}:
            return
        if "id" in value and method:
            # Noninteractive execution cannot invent an approval or tool response.
            self.status = 1
            print(
                "Task requires input. Resume the saved conversation in an interactive terminal.",
                file=sys.stderr,
            )
            self.done.set()
            return
        if method == "turn/started":
            self.write({"type": "turn.started"})
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {}).get("last", {})
            self.usage = {
                "input_tokens": usage.get("inputTokens", 0),
                "cached_input_tokens": usage.get("cachedInputTokens", 0),
                "output_tokens": usage.get("outputTokens", 0),
            }
        elif method in {"item/started", "item/completed"}:
            native = params.get("item", {})
            kind = native.get("type")
            translated = {"id": native.get("id")}
            if kind == "agentMessage":
                translated.update(type="agent_message", text=native.get("text", ""))
                if method == "item/completed":
                    self.last_message = native.get("text", "")
                    if not self.json_output:
                        print(self.last_message, flush=True)
            elif kind == "reasoning":
                translated.update(
                    type="reasoning", text="\n".join(native.get("summary", []))
                )
            elif kind == "commandExecution":
                translated.update(
                    type="command_execution",
                    command=native.get("command", ""),
                    aggregated_output=native.get("aggregatedOutput", ""),
                    exit_code=native.get("exitCode"),
                    status="completed" if method == "item/completed" else "in_progress",
                )
            elif kind == "fileChange":
                translated.update(
                    type="file_change",
                    changes=native.get("changes", []),
                    status="completed" if method == "item/completed" else "in_progress",
                )
            elif kind == "mcpToolCall":
                translated.update(
                    type="mcp_tool_call",
                    server=native.get("server", ""),
                    tool=native.get("tool", ""),
                    arguments=native.get("arguments", {}),
                    result=native.get("result"),
                    error=native.get("error"),
                    status="completed" if method == "item/completed" else "in_progress",
                )
            elif kind == "webSearch":
                action = native.get("action") or {}
                translated.update(
                    type="web_search",
                    query=native.get("query")
                    or action.get("query")
                    or action.get("queries", []),
                    action=action,
                )
            elif kind == "plan":
                translated.update(
                    type="todo_list", items=native.get("plan", native.get("items", []))
                )
            elif kind == "collabAgentToolCall":
                translated.update(
                    type="collab_tool_call",
                    tool=native.get("tool"),
                    sender_thread_id=native.get("senderThreadId"),
                    receiver_thread_ids=native.get("receiverThreadIds", []),
                    prompt=native.get("prompt"),
                    agents_states=native.get("agentsStates", {}),
                    status=native.get("status"),
                )
            elif kind in {"imageView", "imageGeneration"}:
                translated.update(
                    type="image_view" if kind == "imageView" else "image_generation",
                    path=native.get("path"),
                    status=native.get("status"),
                )
            else:
                return
            self.write(
                {
                    "type": "item.completed"
                    if method == "item/completed"
                    else "item.started",
                    "item": translated,
                }
            )
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if turn.get("status") == "completed":
                self.write({"type": "turn.completed", "usage": self.usage})
                self.done.set()
            elif turn.get("status") in {"failed", "interrupted"}:
                from pool import failure_kind

                error = turn.get("error") or {}
                recoverable = failure_kind(
                    error.get("codexErrorInfo"), error.get("message")
                )
                if not recoverable:
                    self.status = 1
                    self.write(
                        {
                            "type": "turn.failed",
                            "error": {
                                "message": "Task stopped. Review the saved conversation."
                            },
                        }
                    )
                    self.done.set()


async def run(args, state, binary):
    from pool import file_lock

    async with file_lock(state / "metadata.lock", shared=True):
        return await _run(args, state, binary)


async def _run(args, state, binary):
    from router import (
        Controller,
        RPC,
        RouterError,
        labels,
        account_home,
        resolve_resume_target,
    )
    from broker import ClientAccount
    from pool import Pool
    import settings

    invocation_cwd = os.getcwd()
    arguments = arguments_only(args.codex_args)
    cwd = working_directory(arguments, invocation_cwd)
    arguments, cwd = prepare_worktree(arguments, state, cwd)
    config = options(arguments, invocation_cwd)
    flags, values = config["flags"], config["values"]
    if not Path(cwd).is_dir():
        raise ValueError("Working directory does not exist")
    if "--skip-git-repo-check" not in flags and config["mode"] != "review":
        checked = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True
        )
        if checked.returncode:
            raise ValueError(
                "This directory is not a Git repository; pass --skip-git-repo-check to continue"
            )
    prompt = config["prompt"]
    stdin = ""
    if (
        prompt == "-"
        or prompt is None
        and config["mode"] not in {"fork", "review"}
        or not sys.stdin.isatty()
    ):
        stdin = await asyncio.to_thread(sys.stdin.read)
    if prompt == "-" or prompt is None:
        prompt = stdin
    elif stdin:
        prompt += "\n<stdin>\n" + stdin + "\n</stdin>"
    if not prompt and config["mode"] not in {"fork", "review"}:
        raise ValueError("Provide a prompt or pipe instructions to stdin")
    _, overrides = translate(arguments)
    from launch import profile_config

    private_config = profile_config(arguments)
    if private_config:
        overrides["config"] = private_config
    overrides.setdefault("approvalPolicy", "never")
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    pool = Pool(state)
    pool.assign("")
    pool.touch(
        name=args.name or "Codex " + config["mode"],
        cwd=cwd,
        mode="managed",
        recovery_enabled=True,
    )
    enrolled = labels(state)
    if not enrolled:
        pool.close()
        raise RouterError("Enroll an account first with relay setup")
    output = Output("--json" in flags)
    backend = None
    controller = None
    heartbeat = None
    try:

        async def factory():
            return await RPC.start(
                binary, home, extra_args=backend_arguments(arguments)
            )

        backend = await factory()
        controller = Controller(
            [
                ClientAccount(label, account_home(state, label), binary, pool)
                for label in enrolled
            ],
            backend,
            pool=pool,
            report=lambda message: print(message, file=sys.stderr),
        )
        controller.background = True
        controller.project = cwd
        controller.profile = args.profile
        controller.preferences = settings.load(state, cwd, args.profile)
        controller.retry_interval = controller.preferences["retry_interval"]
        controller.retry_attempts = controller.preferences["retry_attempts"]
        controller.interval = controller.preferences["unknown_reset_interval"]
        controller.backend_factory = factory
        controller.launch_overrides = overrides
        controller.client = output
        output.controller = controller
        backend.handler = controller.event
        init = await backend.initialize()
        heartbeat = asyncio.create_task(controller.maintain())
        model = values.get("--model")
        first = enrolled.index(args.first) if args.first in enrolled else None
        while True:
            index, deadline = await controller.choose(model, preferred=first)
            if index is None:
                await controller.wait_until(deadline, controller.user_generation)
                continue
            try:
                await controller.login(index)
                break
            except (RouterError, asyncio.TimeoutError):
                await controller.mark(index, model, "unavailable")
        roots = additional_roots(arguments, cwd)
        thread_params = {"cwd": cwd, **overrides}
        if model:
            thread_params["model"] = model
        if roots:
            effective = await backend.call(
                "config/read", {"cwd": cwd, "includeLayers": False}
            )
            configured = (
                effective.get("config", {}).get("sandbox_workspace_write") or {}
            ).get("writable_roots", [])
            thread_params["runtimeWorkspaceRoots"] = list(
                dict.fromkeys([cwd, *configured, *roots])
            )
        if config["mode"] == "fork":
            if not prompt and (
                flags
                & {
                    "--ephemeral",
                    "--image",
                    "--output-schema",
                    "--output-last-message",
                    "--cyber-access-program",
                }
            ):
                raise ValueError(
                    "A fork needs a prompt when images, output options, ephemeral mode, or an access program are requested"
                )
            if "--ephemeral" in flags:
                thread_params["ephemeral"] = True
            if "--thread-source" in values:
                thread_params["threadSource"] = values["--thread-source"]
        if config["mode"] in {"resume", "fork"}:
            selection = [config["mode"]]
            if config["target"]:
                selection.append(config["target"])
            selection.extend(
                flag
                for flag in ("--last", "--all", "--include-non-interactive")
                if flag in flags
            )
            ident = await resolve_resume_target(selection, home, cwd, backend=backend)
            if not ident:
                raise RouterError("No matching saved conversation")
            if config["mode"] == "resume" and pool.owner(ident) not in {
                None,
                pool.session,
            }:
                raise RouterError(
                    "Conversation is already running in another Relay session"
                )
            result = await backend.call(
                "thread/" + config["mode"], {"threadId": ident, **thread_params}
            )
        else:
            if "--ephemeral" in flags:
                thread_params["ephemeral"] = True
            if "--thread-source" in values:
                thread_params["threadSource"] = values["--thread-source"]
            else:
                thread_params["threadSource"] = "exec"
            result = await backend.call("thread/start", thread_params)
        thread = result["thread"]["id"]
        if result["thread"].get("canAcceptDirectInput") is False or not pool.claim(
            thread
        ):
            raise RouterError(
                "Conversation is open in another backend. Close it there or fork it."
            )
        controller.last_turn[thread] = {"threadId": thread}
        controller.merge_settings(thread, result)
        controller.merge_settings(thread, thread_params)
        controller.persist(thread, role="foreground")
        pool.touch(
            thread=thread,
            threads=[thread],
            cwd=cwd,
            model=controller.model_for(thread),
            state="idle",
        )
        output.thread = thread
        output.write({"type": "thread.started", "thread_id": thread})
        if config["mode"] == "fork" and not prompt:
            return 0
        if config["mode"] == "review":
            choices = [
                key for key in ("--uncommitted", "--base", "--commit") if key in flags
            ]
            if len(choices) > 1 or choices and prompt:
                raise ValueError(
                    "Choose one review target, or provide custom review instructions"
                )
            if "--base" in values:
                target = {"type": "baseBranch", "branch": values["--base"]}
            elif "--commit" in values:
                target = {
                    "type": "commit",
                    "sha": values["--commit"],
                    "title": values.get("--title"),
                }
            elif prompt:
                target = {"type": "custom", "instructions": prompt}
            else:
                target = {"type": "uncommittedChanges"}
            reviewed = await backend.call(
                "review/start",
                {"threadId": thread, "target": target, "delivery": "inline"},
            )
            review_thread = reviewed.get("reviewThreadId", thread)
            if review_thread != thread:
                if not pool.claim(review_thread):
                    raise RouterError("Review conversation is already owned")
                controller.last_turn[review_thread] = {"threadId": review_thread}
                controller.merge_settings(
                    review_thread, controller.thread_settings.get(thread, {})
                )
                thread = review_thread
                output.thread = thread
        else:
            inputs = [{"type": "text", "text": prompt, "text_elements": []}]
            for image_group in values.get("--image", []):
                for image in image_group.split(","):
                    path = Path(image).expanduser()
                    inputs.append(
                        {
                            "type": "localImage",
                            "path": str(
                                (
                                    path
                                    if path.is_absolute()
                                    else Path(invocation_cwd) / path
                                ).resolve()
                            ),
                        }
                    )
            turn = {"threadId": thread, "input": inputs}
            if "--output-schema" in values:
                turn["outputSchema"] = json.loads(
                    Path(values["--output-schema"]).read_text()
                )
            if "--cyber-access-program" in values:
                turn["cyberAccessProgram"] = values["--cyber-access-program"]
            await controller.forward(
                {"id": "exec-start", "method": "turn/start", "params": turn}, init
            )
        while not output.done.is_set():
            try:
                await asyncio.wait_for(output.done.wait(), 0.5)
            except asyncio.TimeoutError:
                saved = pool.intent(thread) or {}
                if saved.get("review_required"):
                    raise RouterError(
                        "Task needs review. Resume the saved conversation before retrying."
                    )
                if controller.stopping.is_set():
                    return 130
        if "--output-last-message" in values:
            Path(values["--output-last-message"]).write_text(output.last_message + "\n")
        return output.status
    finally:
        if controller:
            controller.stopping.set()
            for task in (controller.recovery, controller.restart_task, heartbeat):
                if task:
                    task.cancel()
            await asyncio.gather(
                *[
                    task
                    for task in (
                        controller.recovery,
                        controller.restart_task,
                        heartbeat,
                    )
                    if task
                ],
                return_exceptions=True,
            )
            if controller.awake_process and controller.awake_process.returncode is None:
                controller.awake_process.terminate()
                await controller.awake_process.wait()
            await controller.backend.close()
        elif backend:
            await backend.close()
        pool.close()
