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
    }
    if not Path(spec["cwd"]).is_dir():
        raise ValueError("Working directory does not exist")
    if not interactive:
        headless_options(spec["arguments"], spec["cwd"])
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

    ident = resolve(state, prefix)
    root = folder(state)
    # Frontend and backend must keep the same native runtime when the global CLI
    # is updated while this task remains alive.
    spec = json.loads((root / (ident + ".job.json")).read_text())
    binary = spec.get("binary") or binary
    meta = root / (ident + ".connection.json")
    deadline = time.monotonic() + 60
    last_message = None
    while not meta.exists() and (wait_forever or time.monotonic() < deadline):
        spec = json.loads((root / (ident + ".job.json")).read_text())
        if spec.get("state") in {"stopped", "needs_attention"}:
            raise RouterError(
                "Session stopped or needs attention. Check events and doctor."
            )
        if wait_forever:
            pool = Pool(state)
            try:
                detail = next((s for s in pool.detail() if s["id"] == ident), {})
            finally:
                pool.close()
            message = (detail.get("state"), detail.get("next_wake"))
            if message != last_message:
                if message[1]:
                    print(
                        "Waiting until "
                        + time.strftime("%m-%d %H:%M:%S", time.localtime(message[1]))
                        + " for account allowance.",
                        flush=True,
                    )
                last_message = message
        await asyncio.sleep(0.5)
    if not meta.exists():
        raise RouterError(
            "Session is still waiting for an account. Check status; attach when it is ready."
        )
    value = json.loads(meta.read_text())
    if not Pool.alive(value["pid"]) or value.get("birth") != process_birth(
        value["pid"]
    ):
        raise RouterError(
            "This session connection is stale. The supervisor is recovering it; check status."
        )
    env = clean_env(Path(value["home"]))
    env["RELAY_TRANSPORT_TOKEN"] = value["token"]
    # Permission settings belong to the server's persisted task. They are not
    # passed as unsupported resume overrides to the native remote frontend.
    args = arguments or ["resume", value["thread"]]
    if not arguments and not value.get("thread"):
        args = []
    from launch import translate, frontend_arguments

    invocation_cwd = cwd or value.get("cwd") or os.getcwd()
    args = frontend_arguments(args, invocation_cwd)
    args, overrides = translate(args)
    if overrides:
        await send_control(state, ident, "permissions", overrides)
    if takeover:
        await send_control(state, ident, "detach")
        value = json.loads(meta.read_text())
        env["RELAY_TRANSPORT_TOKEN"] = value["token"]
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
    try:
        while proc.returncode is None:
            try:
                return await asyncio.wait_for(asyncio.shield(waiter), 1)
            except asyncio.TimeoutError:
                pass
            try:
                latest = json.loads(meta.read_text())
                if (
                    latest.get("pid") == value["pid"]
                    and latest.get("token") != value["token"]
                ):
                    # A takeover moved the attachment. Prevent the old native
                    # frontend from reconnecting forever with its expired token.
                    return 0
            except FileNotFoundError:
                if (
                    json.loads((root / (ident + ".job.json")).read_text()).get("state")
                    == "stopped"
                ):
                    return 0
        return proc.returncode
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(waiter), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        await asyncio.gather(waiter, return_exceptions=True)


def headless_options(args, cwd):
    """Common background launch options. Interactive run still uses native parsing."""
    from launch import translate

    frontend, thread = translate(args)
    backend = []
    i = 0
    while i < len(frontend):
        arg = frontend[i]
        if arg in ("-p", "--profile") or arg.startswith("--profile="):
            raise ValueError(
                "Native profiles need an interactive launch: relay run -- -p PROFILE"
            )
        if arg in ("-m", "--model", "-C", "--cd", "-c", "--config"):
            if i + 1 == len(frontend):
                raise ValueError(arg + " requires a value")
            value = frontend[i + 1]
            i += 2
            if arg in ("-m", "--model"):
                thread["model"] = value
            elif arg in ("-C", "--cd"):
                cwd = str(Path(value).resolve())
            else:
                backend.extend([arg, value])
        elif arg == "--search":
            backend.extend(["-c", 'web_search="live"'])
            i += 1
        else:
            raise ValueError(
                "Background start does not accept "
                + arg
                + "; use --resume/--prompt or attach for interactive CLI options"
            )
    return cwd, thread, backend


async def serve(state, ident, binary):
    from pool import file_lock
    from router import RouterError

    async with file_lock(state / "metadata.lock", shared=True):
        try:
            return await _serve(state, ident, binary)
        except RouterError as error:
            job = folder(state) / (ident + ".job.json")
            spec = json.loads(job.read_text())
            spec.update(state="needs_attention", last_error=str(error))
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
    spec.update(state="starting")
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
        backend_args = backend_arguments(frontend)
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
    controller.preferences = settings.load(state, cwd, spec.get("profile"))
    controller.retry_interval = controller.preferences["retry_interval"]
    controller.retry_attempts = controller.preferences["retry_attempts"]
    controller.interval = controller.preferences["unknown_reset_interval"]
    backend.handler = controller.event
    heartbeat = asyncio.create_task(controller.maintain())
    connection = root / (ident + ".connection.json")
    try:
        init_result = await backend.initialize()
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
                pool.touch(state="waiting_for_reset", next_wake=deadline)
                await controller.wait_until(deadline, controller.user_generation)
        if controller.stopping.is_set():
            return 0
        controller.backend_factory = factory
        thread_id = old_thread or spec.get("resume")
        if thread_id:
            params = {"threadId": thread_id, **overrides}
            if old:
                controller.merge_settings(thread_id, persisted.get("settings", {}))
                params.update(
                    {
                        k: v
                        for k, v in controller.thread_settings[thread_id].items()
                        if k
                        in {
                            "model",
                            "cwd",
                            "approvalPolicy",
                            "sandbox",
                            "permissions",
                            "serviceTier",
                        }
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
                atomic_json(connection, meta)
                # Preserve the accepted conversation identity across worker restart.
                latest = json.loads(spec_path.read_text())
                latest["resume"] = ident
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
                if state_name == "completed":
                    controller.persist(
                        thread_id,
                        state="completed",
                        accepted=True,
                        active_items=False,
                        next_wake=None,
                        kind=None,
                        episode=None,
                    )
                elif persisted.get("active_items") or not persisted.get("accepted"):
                    controller.paused_uncertain.add(thread_id)
                    controller.note(
                        "needs_attention",
                        reason="worker_restarted_during_uncertain_action",
                        thread=thread_id,
                    )
                elif persisted.get("state") in {"retry_scheduled", "waiting_for_reset"}:
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
        spec["state"] = "stopped"
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
        await controller.backend.close()
        pool.close()
        connection.unlink(missing_ok=True)


async def send_control(state, ident, op, params=None):
    from router import RouterError

    pool = Pool(state)
    try:
        command = pool.command(ident, op, params)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with pool.db() as db:
                row = db.execute(
                    "SELECT status FROM commands WHERE id=?", (command,)
                ).fetchone()
            if row and row[0] == "done":
                return
            if row and row[0] == "failed":
                raise RouterError(
                    "Session is busy or rejected that control; no permission change was assumed"
                )
            await asyncio.sleep(0.1)
        raise RouterError("Session control timed out; check status before retrying")
    finally:
        pool.close()
