"""Detached session supervision and attachment metadata. No public service."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

from settings import atomic_json
from launch import RESUME_FIELDS
from pool import Pool, process_birth
from processes import spawn


def folder(state):
    root = state / "services"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def start(
    state,
    *,
    name=None,
    cwd=None,
    first=None,
    arguments=None,
    resume=None,
    prompt=None,
    profile=None,
    interactive=False,
    binary=None,
):
    ident = uuid.uuid4().hex
    root = folder(state)
    spec = {
        "id": ident,
        "name": name or "Codex task",
        "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "first": first,
        "arguments": arguments or [],
        "resume": resume,
        "prompt": prompt,
        "profile": profile,
        "created": time.time(),
        "state": "starting",
        "interactive": interactive,
        "binary": binary,
        "relay_runtime": str(Path(__file__).parent.resolve()),
        "foreground_owner": {"pid": os.getpid(), "birth": process_birth(os.getpid())}
        if interactive
        else None,
        "background_launch": not interactive,
    }
    if not Path(spec["cwd"]).is_dir():
        raise ValueError("Working directory does not exist")
    if not interactive:
        effective_cwd, _, _ = headless_options(spec["arguments"], spec["cwd"])
        from launch import frontend_arguments

        spec["arguments"] = frontend_arguments(spec["arguments"], spec["cwd"])
        spec["cwd"] = effective_cwd
        if not Path(effective_cwd).is_dir():
            raise ValueError("Working directory does not exist")
    atomic_json(root / (ident + ".job.json"), spec)
    process = spawn(
        [
            sys.executable,
            str(Path(__file__).with_name("router.py")),
            "_supervise",
            ident,
        ],
        env={
            **os.environ,
            "CODEX_ACCOUNTS_STATE": str(state),
            "RELAY_STATE": str(state),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=spec["cwd"],
    )
    atomic_json(
        root / (ident + ".supervisor.json"),
        {"pid": process.pid, "birth": process_birth(process.pid)},
    )
    return ident


def resolve(state, prefix):
    candidates = list(
        {
            p.name.split(".")[0]
            for pattern in ["*.job.json", "*.connection.json"]
            for p in folder(state).glob(pattern)
            if p.name.startswith(prefix)
        }
    )
    if len(candidates) != 1:
        raise ValueError("Session prefix is missing or ambiguous")
    return candidates[0]


async def supervise(state, ident):
    root = folder(state)
    spec_path = root / (ident + ".job.json")
    delay = 1
    crashes = []
    while spec_path.exists():
        spec = json.loads(spec_path.read_text())
        if spec.get("state") in {"stopped", "removed"}:
            return 0
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).with_name("router.py")),
            "_serve",
            ident,
            env={
                **os.environ,
                "CODEX_ACCOUNTS_STATE": str(state),
                "RELAY_STATE": str(state),
            },
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        code = await process.wait()
        if code == 0:
            return 0
        pool = Pool(state)
        pool.session = ident
        pool.record("worker_restart", reason="worker_exit", delay=delay)
        pool.close()
        crashes = [t for t in crashes if time.time() - t < 120]
        crashes.append(time.time())
        if code == 2 or len(crashes) >= 5:
            spec = json.loads(spec_path.read_text())
            spec["state"] = "needs_attention"
            atomic_json(spec_path, spec)
            return 0
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)
    return 0


async def attach(
    state, prefix, binary, arguments=None, wait_forever=False, takeover=False, cwd=None
):
    from router import RouterError, clean_env
    from launch import translate, frontend_arguments

    ident = resolve(state, prefix)
    root = folder(state)
    spec_path = root / (ident + ".job.json")
    spec = json.loads(spec_path.read_text())
    binary = spec.get("binary") or binary
    owner = spec.get("foreground_owner")
    if (
        spec.get("interactive")
        and not spec.get("background_launch")
        and owner
        and not (
            Pool.alive(owner["pid"])
            and process_birth(owner["pid"]) == owner.get("birth")
        )
    ):
        spec["foreground_owner"] = {
            "pid": os.getpid(),
            "birth": process_birth(os.getpid()),
        }
        atomic_json(spec_path, spec)
    meta = root / (ident + ".connection.json")
    invocation_cwd = cwd
    reconnect = False

    async def connection(previous=None):
        deadline = time.monotonic() + 60
        last_message = None
        while wait_forever or time.monotonic() < deadline:
            job = json.loads(spec_path.read_text())
            if job.get("state") in {"stopped", "needs_attention", "removed"}:
                raise RouterError(
                    "Session stopped or needs attention. Check status and events."
                )
            try:
                candidate = json.loads(meta.read_text())
                if (
                    Pool.alive(candidate["pid"])
                    and candidate.get("birth") == process_birth(candidate["pid"])
                    and (
                        not previous
                        or any(
                            candidate.get(k) != previous.get(k)
                            for k in ("pid", "birth", "token")
                        )
                    )
                ):
                    return candidate
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if wait_forever:
                pool = Pool(state)
                try:
                    detail = next((s for s in pool.detail() if s["id"] == ident), {})
                finally:
                    pool.close()
                message = (detail.get("state"), detail.get("next_wake"))
                if message != last_message and message[1]:
                    signin = detail.get("sign_in_required", [])
                    print(
                        (
                            "Sign-in required for "
                            + ", ".join(signin)
                            + ". Run relay account reauth LABEL. Next availability check: "
                            if signin
                            else "Waiting until "
                        )
                        + time.strftime("%m-%d %H:%M:%S", time.localtime(message[1]))
                        + ("." if signin else " for account availability."),
                        flush=True,
                    )
                last_message = message
            await asyncio.sleep(0.5)
        raise RouterError(
            "Session is still recovering or waiting for an account. Check status and attach when ready."
        )

    value = await connection()
    while True:
        env = clean_env(Path(value["home"]))
        env["RELAY_TRANSPORT_TOKEN"] = value["token"]
        if reconnect:
            args = ["resume", value["thread"]] if value.get("thread") else []
            if "--no-alt-screen" in (arguments or []):
                args.append("--no-alt-screen")
            invocation_cwd = value.get("cwd") or invocation_cwd
        else:
            args = (
                arguments
                if arguments is not None
                else (["resume", value["thread"]] if value.get("thread") else [])
            )
        invocation_cwd = invocation_cwd or value.get("cwd") or os.getcwd()
        args, overrides = translate(frontend_arguments(args, invocation_cwd))
        if overrides:
            await send_control(state, ident, "permissions", overrides)
        if takeover and not reconnect:
            await send_control(state, ident, "detach")
            value = json.loads(meta.read_text())
            env["RELAY_TRANSPORT_TOKEN"] = value["token"]
        from terminal import TerminalState

        terminal_state = TerminalState()
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--remote",
            value["url"],
            "--remote-auth-token-env",
            "RELAY_TRANSPORT_TOKEN",
            *args,
            env=env,
            cwd=invocation_cwd,
        )
        waiter = asyncio.create_task(proc.wait())
        replacement = False
        missing = False
        try:
            while True:
                try:
                    latest = json.loads(meta.read_text())
                    if latest.get("pid") != value["pid"] or latest.get(
                        "birth"
                    ) != value.get("birth"):
                        replacement = True
                        break
                    if latest.get("token") != value["token"]:
                        # Explicit takeover closes the old terminal, not the task.
                        return 0
                except (FileNotFoundError, json.JSONDecodeError):
                    missing = True
                    if json.loads(spec_path.read_text()).get("state") in {
                        "stopped",
                        "needs_attention",
                    }:
                        return 0
                try:
                    result = await asyncio.wait_for(asyncio.shield(waiter), 0.5)
                    if missing:
                        replacement = True
                        break
                    return result
                except asyncio.TimeoutError:
                    pass
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), 5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            await asyncio.gather(waiter, return_exceptions=True)
            terminal_state.restore()
        if replacement:
            print("Reconnecting to the recovered task…", flush=True)
            value = await connection(previous=value)
            reconnect = True


def headless_options(args, cwd):
    """Use the same native option boundaries for background and interactive work."""
    from launch import translate, tokens, working_directory, backend_arguments

    frontend, thread = translate(args)
    cwd = working_directory(frontend, cwd)
    supported = {
        "--model",
        "--cd",
        "--config",
        "--profile",
        "--enable",
        "--disable",
        "--search",
        "--strict-config",
        "--add-dir",
    }
    for name, value, raw in tokens(frontend):
        if name not in supported:
            raise ValueError(
                "Background start does not accept "
                + raw[0]
                + "; use --resume/--prompt or relay run -- for native CLI commands"
            )
        if name == "--model":
            thread["model"] = value
    return cwd, thread, backend_arguments(args)


async def serve(state, ident, binary):
    from pool import file_lock
    from router import RouterError

    async with file_lock(state / "metadata.lock", shared=True):
        try:
            return await _serve(state, ident, binary)
        except (RouterError, ValueError, OSError) as error:
            job = folder(state) / (ident + ".job.json")
            spec = json.loads(job.read_text())
            spec.update(
                state="needs_attention",
                last_error=str(error)
                if isinstance(error, RouterError)
                else "Launch settings or project files are unavailable. Check relay explain and relay doctor.",
            )
            atomic_json(job, spec)
            return 2


def restart(state, prefix):
    from router import RouterError

    ident = resolve(state, prefix)
    root = folder(state)
    pool = Pool(state)
    try:
        if any(s["id"] == ident for s in pool.detail()):
            raise RouterError("That session is already running")
    finally:
        pool.close()
    supervisor = root / (ident + ".supervisor.json")
    if supervisor.exists():
        value = json.loads(supervisor.read_text())
        if Pool.alive(value["pid"]) and process_birth(value["pid"]) == value.get(
            "birth"
        ):
            raise RouterError("That session is already recovering; check status")
    path = root / (ident + ".job.json")
    spec = json.loads(path.read_text())
    if not Path(spec["cwd"]).is_dir():
        raise RouterError("The project folder no longer exists")
    spec.update(
        state="starting",
        background_launch=True,
        foreground_owner=None,
        relay_runtime=str(Path(__file__).parent.resolve()),
    )
    spec.pop("last_error", None)
    atomic_json(path, spec)
    process = spawn(
        [
            sys.executable,
            str(Path(__file__).with_name("router.py")),
            "_supervise",
            ident,
        ],
        env={
            **os.environ,
            "CODEX_ACCOUNTS_STATE": str(state),
            "RELAY_STATE": str(state),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=spec["cwd"],
    )
    atomic_json(supervisor, {"pid": process.pid, "birth": process_birth(process.pid)})
    return ident


def foreground_owner_gone(spec, preferences, controller, *, now=None):
    """Foreground jobs must not outlive a vanished launcher unless explicitly detached."""
    if (
        not spec.get("interactive")
        or spec.get("background_launch")
        or spec.get("takeover")
        or preferences.get("background_on_disconnect")
        or controller.client
    ):
        return False
    owner = spec.get("foreground_owner")
    if owner:
        return not (
            Pool.alive(owner["pid"])
            and process_birth(owner["pid"]) == owner.get("birth")
        )
    # Earlier jobs did not record a launcher. Only retire a definitely empty,
    # unattached picker; retain older conversations whose ownership is uncertain.
    return (
        not spec.get("resume")
        and not controller.last_turn
        and (time.time() if now is None else now) - spec.get("created", time.time())
        > 60
    )


async def close_orphaned_foreground(controller, spec_path):
    spec = json.loads(spec_path.read_text())
    if not foreground_owner_gone(spec, controller.preferences, controller):
        return
    if (
        controller.active_threads
        or controller.active_items
        or controller.server_requests
    ):
        for thread in controller.last_turn:
            controller.hold_for_review(thread, "terminal_closed_during_action")
        controller.exit_state = "needs_attention"
        if not getattr(controller, "orphan_reported", False):
            controller.note("needs_attention", reason="terminal_closed_during_action")
            controller.orphan_reported = True
        return
    controller.note("foreground_closed", reason="launcher_exited")
    spec["state"] = controller.exit_state
    atomic_json(spec_path, spec)
    controller.stopping.set()


async def _serve(state, ident, binary):
    from websockets.asyncio.server import serve as ws_serve
    from router import (
        RPC,
        Controller,
        labels,
        account_home,
        transport_guard,
        RouterError,
    )
    from broker import ClientAccount
    import settings

    root = folder(state)
    spec_path = root / (ident + ".job.json")
    spec = json.loads(spec_path.read_text())
    binary = spec.get("binary") or binary
    if spec.get("interactive"):
        from launch import translate, backend_arguments

        frontend, overrides = translate(spec.get("arguments", []))
        cwd = spec["cwd"]
        backend_args = backend_arguments(spec.get("arguments", []))
    else:
        cwd, overrides, backend_args = headless_options(
            spec.get("arguments", []), spec["cwd"]
        )
    pool = Pool(state)
    pool.session = ident
    pool.assign("")
    pool.touch(name=spec["name"], cwd=cwd, state="starting", background=True)
    enrolled = labels(state)
    if not enrolled:
        raise RouterError(
            "Enroll at least one account before starting a background task"
        )
    accounts = [
        ClientAccount(label, account_home(state, label), binary, pool)
        for label in enrolled
    ]
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    factory = lambda: RPC.start(binary, home, extra_args=backend_args)
    backend = await factory()
    controller = Controller(accounts, backend, pool=pool, report=lambda message: None)
    with pool.db() as db:
        saved = db.execute(
            "SELECT details FROM session_details WHERE id=?", (ident,)
        ).fetchone()
    controller.paused = bool(json.loads(saved[0]).get("paused")) if saved else False
    controller.background = True
    controller.starting = True
    controller.launch_overrides = overrides
    from launch import profile_config

    private_config = profile_config(spec.get("arguments", []))
    if private_config:
        controller.launch_overrides["config"] = private_config
    from launch import additional_roots

    controller.additional_roots = additional_roots(spec.get("arguments", []), cwd)
    controller.project = cwd
    controller.profile = spec.get("profile")

    async def reload_accounts(old):
        current = controller.account.label
        mapping = {a.label: a for a in old}
        new = [
            mapping.get(label)
            or ClientAccount(label, account_home(state, label), binary, pool)
            for label in labels(state)
        ]
        if new:
            controller.index = next(
                (i for i, a in enumerate(new) if a.label == current), 0
            )
        return new or old

    controller.extra_accounts = reload_accounts
    controller.lifecycle_check = lambda: close_orphaned_foreground(
        controller, spec_path
    )
    controller.preferences = settings.load(state, cwd, spec.get("profile"))
    controller.retry_interval = controller.preferences["retry_interval"]
    controller.retry_attempts = controller.preferences["retry_attempts"]
    controller.interval = controller.preferences["unknown_reset_interval"]
    backend.handler = lambda message: controller.event(message, origin=backend)
    heartbeat = asyncio.create_task(controller.maintain())
    connection = root / (ident + ".connection.json")
    try:
        init_result = await backend.initialize()
        if controller.additional_roots:
            effective = await backend.call(
                "config/read", {"cwd": cwd, "includeLayers": False}
            )
            configured = (
                effective.get("config", {}).get("sandbox_workspace_write") or {}
            ).get("writable_roots", [])
            controller.additional_roots = list(
                dict.fromkeys([*configured, *controller.additional_roots])
            )
        # Restore configuration-only recovery intent from a previous worker.
        with pool.db() as db:
            old = db.execute(
                "SELECT thread,details FROM intents WHERE owner=?", (ident,)
            ).fetchall()
        old = [
            row
            for row in old
            if json.loads(row[1]).get("role", "foreground") == "foreground"
        ]
        old_thread = (spec.get("resume") if old else None) or (
            old[-1][0] if old else None
        )
        persisted = json.loads(next((r[1] for r in old if r[0] == old_thread), "{}"))
        preferred = next(
            (
                i
                for i, a in enumerate(accounts)
                if a.label == (persisted.get("account_label") or spec.get("first"))
            ),
            None,
        )
        while not controller.stopping.is_set():
            index, deadline = await controller.choose(
                model=overrides.get("model")
                or persisted.get("settings", {}).get("model"),
                preferred=preferred,
            )
            if index is not None:
                try:
                    await controller.login(index)
                    break
                except (RouterError, asyncio.TimeoutError):
                    await controller.mark(index, None, "unavailable")
            else:
                controller.waiting_accounts(deadline)
                await controller.wait_until(deadline, controller.user_generation)
        if controller.stopping.is_set():
            latest = json.loads(spec_path.read_text())
            latest["state"] = controller.exit_state
            atomic_json(spec_path, latest)
            return 0
        controller.backend_factory = factory
        thread_id = old_thread or spec.get("resume")
        if thread_id:
            params = {"threadId": thread_id, "excludeTurns": True, **overrides}
            if old:
                controller.merge_settings(thread_id, persisted.get("settings", {}))
                params.update(
                    {
                        k: v
                        for k, v in controller.thread_settings[thread_id].items()
                        if k in RESUME_FIELDS
                    }
                )
            result = await backend.call("thread/resume", params)
        elif not spec.get("interactive"):
            result = await backend.call("thread/start", {"cwd": cwd, **overrides})
            thread_id = result["thread"]["id"]
        else:
            result = {}
        if thread_id:
            if result.get("thread", {}).get("canAcceptDirectInput") is False:
                raise RouterError(
                    "Conversation is owned by another backend; no work was stopped"
                )
            if not pool.claim(thread_id):
                raise RouterError(
                    "Conversation already belongs to another wrapper session"
                )
            controller.last_turn[thread_id] = {"threadId": thread_id}
            controller.merge_settings(thread_id, result)
            controller.merge_settings(thread_id, overrides)
            controller.persist(thread_id, role="foreground")
        controller.starting = False
        pool.touch(
            thread=thread_id,
            threads=[thread_id] if thread_id else [],
            state="idle",
            model=controller.model_for(thread_id) if thread_id else None,
        )
        token = secrets.token_urlsafe(32)
        meta = {"token": token}

        async def handle(ws):
            await controller.connect(ws, init_result)

        async with ws_serve(
            handle,
            "127.0.0.1",
            0,
            process_request=transport_guard(lambda: meta["token"]),
            max_size=32 * 1024 * 1024,
        ) as server:
            port = server.sockets[0].getsockname()[1]
            meta = {
                "pid": os.getpid(),
                "birth": process_birth(os.getpid()),
                "token": token,
                "url": f"ws://127.0.0.1:{port}",
                "thread": thread_id,
                "home": str(home),
                "cwd": cwd,
            }

            async def selected(ident, values):
                nonlocal thread_id
                thread_id = ident
                meta["thread"] = ident
                effective_cwd = values.get("cwd")
                if effective_cwd:
                    meta["cwd"] = effective_cwd
                    controller.project = effective_cwd
                    controller.preferences = settings.load(
                        state, effective_cwd, controller.profile
                    )
                    controller.retry_interval = controller.preferences["retry_interval"]
                    controller.retry_attempts = controller.preferences["retry_attempts"]
                    controller.interval = controller.preferences[
                        "unknown_reset_interval"
                    ]
                atomic_json(connection, meta)
                # Preserve the accepted conversation identity across worker restart.
                latest = json.loads(spec_path.read_text())
                latest["resume"] = ident
                if effective_cwd:
                    latest["cwd"] = effective_cwd
                latest["state"] = "running"
                spec.update(latest)
                atomic_json(spec_path, spec)

            controller.on_thread = selected

            async def detach():
                meta["token"] = secrets.token_urlsafe(32)
                atomic_json(connection, meta)

            controller.on_detach = detach
            if spec.get("prompt") and not old_thread:
                request = {
                    "id": "headless-start",
                    "method": "turn/start",
                    "params": {
                        "threadId": thread_id,
                        "input": [
                            {
                                "type": "text",
                                "text": spec["prompt"],
                                "text_elements": [],
                            }
                        ],
                    },
                }
                # Preserve the initial prompt privately until native acceptance is
                # known. Do not put it in diagnostic events or status output.
                await controller.forward(request, init_result)
                if controller.pending_intents.get(thread_id, {}).get("accepted"):
                    spec["prompt"] = None
                    spec["resume"] = thread_id
                    spec["state"] = "running"
                    atomic_json(spec_path, spec)
            elif old_thread:
                state_name, turn = await controller.reconcile(
                    thread_id,
                    baseline=persisted.get("baseline")
                    if not persisted.get("accepted")
                    else None,
                )
                if state_name == "completed" and not persisted.get("rejected"):
                    controller.persist(
                        thread_id,
                        state="completed",
                        accepted=True,
                        active_items=False,
                        next_wake=None,
                        kind=None,
                        episode=None,
                    )
                elif (
                    persisted.get("review_required")
                    or persisted.get("rejected")
                    or persisted.get("active_items")
                    or not persisted.get("accepted")
                ):
                    controller.hold_for_review(thread_id)
                    controller.note(
                        "needs_attention",
                        reason="worker_restarted_during_uncertain_action",
                        thread=thread_id,
                    )
                elif persisted.get("state") in {
                    "retry_scheduled",
                    "waiting_for_reset",
                    "waiting_for_accounts",
                }:
                    controller.recovery = asyncio.create_task(
                        controller.restore_recovery(thread_id, persisted)
                    )
                elif state_name == "failed" and turn:
                    from pool import failure_kind

                    kind = failure_kind(
                        (turn.get("error") or {}).get("codexErrorInfo"),
                        (turn.get("error") or {}).get("message"),
                    )
                    if kind:
                        controller.recovery = asyncio.create_task(
                            controller.recover(
                                thread_id, controller.user_generation, kind
                            )
                        )
                elif persisted.get("state") not in {"completed", "idle"}:
                    controller.recovery = asyncio.create_task(
                        controller.continue_thread(
                            thread_id, controller.user_generation
                        )
                    )
            # Publish the attachment only after accepted work and uncertainty
            # have been reconciled. A frontend must not race recovery setup.
            atomic_json(connection, meta)
            await controller.stopping.wait()
        spec["state"] = controller.exit_state
        atomic_json(spec_path, spec)
        return 0
    finally:
        controller.stopping.set()
        heartbeat.cancel()
        for task in (controller.recovery, controller.restart_task):
            if task:
                task.cancel()
        await asyncio.gather(
            heartbeat,
            *[t for t in (controller.recovery, controller.restart_task) if t],
            return_exceptions=True,
        )
        if controller.awake_process and controller.awake_process.returncode is None:
            controller.awake_process.terminate()
            await controller.awake_process.wait()
        await controller.backend.close()
        pool.close()
        connection.unlink(missing_ok=True)


async def wait_control(pool, command, timeout=15):
    from router import RouterError, CONTROL_REJECTIONS

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with pool.db() as db:
            row = db.execute(
                "SELECT status,details FROM commands WHERE id=?", (command,)
            ).fetchone()
        if row and row[0] == "done":
            return
        if row and row[0] == "failed":
            raise RouterError(
                CONTROL_REJECTIONS.get(
                    json.loads(row[1]).get("_rejection"),
                    "Session rejected that action. Check status and events; no change was assumed.",
                )
            )
        await asyncio.sleep(0.1)
    raise RouterError("Session control timed out; check status before retrying.")


async def send_control(state, ident, op, params=None):
    pool = Pool(state)
    try:
        await wait_control(pool, pool.command(ident, op, params), timeout=15)
    finally:
        pool.close()
