"""Local management commands. Status and diagnostics never return credentials."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from pool import Pool, file_lock
import settings


def account_identity(state, label):
    from router import account_home

    home = account_home(state, label)
    for name in ("identity.json", "enrolled"):
        path = home / name
        try:
            value = json.loads(path.read_text())
            if value.get("email"):
                return {"email": value["email"]}
        except (OSError, ValueError):
            pass
    return {}


def identify_accounts(state):
    from router import labels, account_home, read_keychain, RouterError

    updated = 0
    for label in labels(state):
        if account_identity(state, label).get("email"):
            continue
        try:
            identity = read_keychain(account_home(state, label), include_identity=True)
        except RouterError:
            continue
        if identity.get("email"):
            settings.atomic_json(
                account_home(state, label) / "identity.json",
                {"email": identity["email"]},
            )
            updated += 1
    return updated


def resolve_label(state, value):
    from router import labels, RouterError

    if value in labels(state):
        return value
    pool = Pool(state)
    try:
        matching = [
            label
            for label in labels(state)
            if pool.options(label).get("name") == value
            or account_identity(state, label).get("email") == value
        ]
        if len(matching) == 1:
            return matching[0]
    finally:
        pool.close()
    raise RouterError("Unknown or ambiguous account name")


def snapshot(state, include_inactive=False):
    from router import labels, VERSION

    pool = Pool(state)
    try:
        sessions = pool.detail()
        accounts = []
        with pool.db() as db:
            blocks = [
                dict(account=r[0], scope=r[1], next_check=r[2], reason=r[3])
                for r in db.execute(
                    "SELECT label,scope,until,reason FROM blocks WHERE until>? ORDER BY until",
                    (time.time(),),
                )
            ]
        for label in labels(state):
            options = pool.options(label)
            identity = account_identity(state, label)
            usage = pool.cached(label, 86400)
            windows = (usage or {}).get("rateLimits", {})
            accounts.append(
                {
                    "id": label,
                    "name": options.get("name") or identity.get("email") or label,
                    "email": identity.get("email"),
                    "enabled": options.get("enabled", True),
                    "priority": options.get("priority", 0),
                    "max_sessions": options.get("max_sessions", 0),
                    "sessions": sum(s["account"] == label for s in sessions),
                    "primary": windows.get("primary"),
                    "secondary": windows.get("secondary"),
                    "usage_known": usage is not None,
                    "usage_checked_at": pool.checked_at(label),
                    "maintenance": options.get("maintenance", False),
                    "next_check": min(
                        (b["next_check"] for b in blocks if b["account"] == label),
                        default=None,
                    ),
                }
            )
        with pool.db() as db:
            for session in sessions:
                account = next(
                    (a for a in accounts if a["id"] == session.get("account")), {}
                )
                session["account_name"] = account.get(
                    "name", session.get("account", "")
                )
                session["account_email"] = account.get("email")
                saved_intent = db.execute(
                    "SELECT details FROM intents WHERE owner=? AND thread=?",
                    (session["id"], session.get("thread", "")),
                ).fetchone()
                intent = json.loads(saved_intent[0]) if saved_intent else {}
                session["reason"] = intent.get("review_reason") or intent.get("kind")
                session["attempt"] = (intent.get("episode") or {}).get("attempt")
                session["review_required"] = intent.get("review_required", False)
                if session["review_required"]:
                    session["state"] = "needs_attention"
                session.setdefault("recovery_enabled", True)
        active = {s["id"] for s in sessions}
        with pool.db() as db:
            saved = {
                r[0]: json.loads(r[1])
                for r in db.execute("SELECT id,details FROM session_details")
            }
        for path in (state / "services").glob("*.job.json"):
            try:
                job = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            ident = job.get("id", path.name.split(".")[0])
            if ident in active:
                continue
            if not include_inactive and job.get("state") != "needs_attention":
                continue
            prior = saved.get(ident, {})
            sessions.append(
                {
                    "id": ident,
                    "pid": None,
                    "name": job.get("name", ident[:10]),
                    "cwd": job.get("cwd"),
                    "thread": job.get("resume"),
                    "account": prior.get("account") or job.get("first") or "",
                    "model": prior.get("model"),
                    "state": job.get("state", "unknown"),
                    "paused": prior.get("paused", False),
                    "last_error": job.get("last_error"),
                    "running": False,
                }
            )
        return {
            "version": VERSION,
            "at": time.time(),
            "sessions": sessions,
            "accounts": accounts,
            "blocks": blocks,
        }
    finally:
        pool.close()


def render(value):
    def percent(window):
        return (
            "?" if not window else str(max(0, 100 - window.get("usedPercent", 0))) + "%"
        )

    lines = [
        f"Relay · {len(value['sessions'])} sessions · {len(value['accounts'])} accounts",
        "",
        f"{'ACCOUNT':<39}{'STATE':<13}{'SHORT LEFT':<13}{'WEEK LEFT':<13}{'SESSIONS':<10}NEXT CHECK",
    ]
    for account in value["accounts"]:
        label = (
            account["name"]
            + (" [" + account["id"] + "]" if account["name"] != account["id"] else "")
        )[:38]
        state = (
            "maintenance"
            if account["maintenance"]
            else "enabled"
            if account["enabled"]
            else "disabled"
        )
        next_at = (
            time.strftime("%m-%d %H:%M:%S", time.localtime(account["next_check"]))
            if account["next_check"]
            else "—"
        )
        lines.append(
            f"{label:<39}{state:<13}{percent(account['primary']):<13}{percent(account['secondary']):<13}{account['sessions']:<10}{next_at}"
        )
    if value["sessions"]:
        lines += [
            "",
            f"{'SESSION':<12}{'ACCOUNT':<32}{'STATE':<23}{'MODEL':<23}PROJECT / NAME",
        ]
        for row in value["sessions"]:
            lines.append(
                f"{row['id'][:10]:<12}{str(row.get('account_name') or row.get('account', '')):<32}{str(row.get('state', 'unknown'))[:22]:<23}{str(row.get('model') or '?')[:22]:<23}{row.get('cwd') or row.get('name') or ''}"
            )
            if row.get("next_wake"):
                lines.append(
                    "  next action: "
                    + time.strftime("%m-%d %H:%M:%S", time.localtime(row["next_wake"]))
                )
    lines += [
        "",
        "Percentages are last observed allowance; native account limits remain authoritative.",
    ]
    return "\n".join(lines)


async def status(args, state):
    from watch import LocalChanges, fingerprint

    previous = None
    watcher = LocalChanges(state) if args.watch else None
    try:
        while True:
            value = snapshot(state, getattr(args, "all", False))
            current = fingerprint(value)
            if current != previous:
                if args.json:
                    print(
                        json.dumps(value, indent=None if args.watch else 2), flush=True
                    )
                else:
                    if args.watch and sys.stdout.isatty():
                        print("\033[2J\033[H", end="")
                    print(render(value), flush=True)
                previous = current
            if not args.watch:
                return 0
            await watcher.changed(max(1, args.interval))
    finally:
        if watcher:
            watcher.close()


async def control(args, state):
    from router import RouterError

    pool = Pool(state)
    try:
        sessions = pool.detail()
        if args.target == "all":
            matching = sessions
        elif args.target:
            matching = [
                s
                for s in sessions
                if s["id"].startswith(args.target) or s.get("name") == args.target
            ]
        else:
            matching = sessions if len(sessions) == 1 else []
        if not matching or args.target != "all" and len(matching) != 1:
            raise RouterError(
                "Choose a unique session ID/name from status, or use all."
            )
        params = {}
        if args.command == "switch":
            params["account"] = resolve_label(state, args.account)
        if getattr(args, "thread", None):
            params["thread"] = args.thread
        for session in matching:
            op = "resume" if args.command == "unpause" else args.command
            ident = pool.command(session["id"], op, params)
            if getattr(args, "wait", False):
                from service import wait_control

                await wait_control(pool, ident, timeout=args.timeout)
                print(f"{op.capitalize()} applied to {session['id'][:10]}.")
            else:
                print(f"Queued {op} for {session['id'][:10]} (command {ident}).")
        return 0
    finally:
        pool.close()


async def manage(args, state, binary):
    async with file_lock(state / "enrollment.lock"):
        return await _manage(args, state, binary)


async def _manage(args, state, binary):
    from router import labels, account_home, add_account, RouterError, clean_env
    import broker

    pool = Pool(state)
    try:
        label = resolve_label(state, args.label) if args.action != "add" else args.label
        if args.action in {"enable", "disable", "priority", "cap", "rename", "models"}:
            if args.action in {"enable", "disable"}:
                pool.set_options(label, enabled=args.action == "enable")
            elif args.action == "priority":
                pool.set_options(label, priority=args.value)
            elif args.action == "cap":
                if args.value < 0:
                    raise RouterError(
                        "Session cap must be nonnegative; 0 is unlimited."
                    )
                pool.set_options(label, max_sessions=args.value)
            elif args.action == "models":
                pool.set_options(label, models=args.values)
            else:
                if not args.value.strip():
                    raise RouterError("Choose a nonempty name")
                if any(
                    pool.options(other).get("name", other) == args.value
                    for other in labels(state)
                    if other != label
                ):
                    raise RouterError("That account name is already used")
                pool.set_options(label, name=args.value)
            print(f"{label}: {args.action} updated.")
            return 0
        home = account_home(state, label)
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not pool.begin_maintenance(label):
            raise RouterError(
                "This account is assigned to a live session. Switch those sessions to another account first; other accounts can keep running."
            )
        try:
            with contextlib.suppress(Exception):
                await broker.exchange(state, {"op": "invalidate", "label": label}, 15)
            async with file_lock(home / "credential.lock"):
                if args.action in {"add", "reauth"}:
                    args.label = label
                    await asyncio.to_thread(add_account, args, state, binary)
                elif args.action == "remove":
                    if args.revoke:
                        result = await asyncio.create_subprocess_exec(
                            binary,
                            "logout",
                            env=clean_env(home),
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        if await result.wait():
                            raise RouterError(
                                "Native sign-out failed; account was retained."
                            )
                    marker = home / "enrolled"
                    if marker.exists():
                        marker.rename(home / "enrolled.removed")
                    pool.reset_identity(label)
                    pool.set_options(label, enabled=False)
                    print(
                        f"{label}: removed from routing. "
                        + (
                            "Native login revoked."
                            if args.revoke
                            else "Keychain login retained; use add to enroll again."
                        )
                    )
        finally:
            pool.set_options(label, maintenance=False)
            pool.signal()
        return 0
    finally:
        pool.close()


def history(state, label=None):
    pool = Pool(state)
    try:
        with pool.db() as db:
            rows = db.execute(
                "SELECT at,label,snapshot FROM history WHERE (? IS NULL OR label=?) ORDER BY at DESC LIMIT 1000",
                (label, label),
            ).fetchall()
        grouped = {}
        for at, name, raw in rows:
            grouped.setdefault(name, []).append((at, json.loads(raw)))
        output = []
        for name, points in grouped.items():
            latest = points[0][1].get("rateLimits", {})
            item = {
                "account": name,
                "samples": len(points),
                "at": points[0][0],
                "primary": latest.get("primary"),
                "secondary": latest.get("secondary"),
                "forecast": None,
            }
            if len(points) > 1:
                first_time, first = points[-1]
                last_time, last = points[0]
                a = first.get("rateLimits", {}).get("primary") or {}
                b = last.get("rateLimits", {}).get("primary") or {}
                elapsed = last_time - first_time
                delta = b.get("usedPercent", 0) - a.get("usedPercent", 0)
                if (
                    elapsed >= 60
                    and delta > 0
                    and a.get("resetsAt") == b.get("resetsAt")
                ):
                    item["forecast"] = {
                        "estimated_seconds_to_depletion": max(0, 100 - b["usedPercent"])
                        * elapsed
                        / delta,
                        "basis": "Observed primary allowance change in one reset window; estimate only.",
                    }
            output.append(item)
        return output
    finally:
        pool.close()


async def doctor(args, state, binary):
    import compatibility

    result = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "state": str(state),
        "checks": {},
    }
    checks = result["checks"]
    if args.restore_backup:
        from metadata import restore_backup

        try:
            async with asyncio.timeout(2):
                async with file_lock(state / "metadata.lock"):
                    try:
                        checks["metadata_before_restore"] = restore_backup(state)
                    except sqlite3.Error:
                        raise ValueError(
                            "Metadata backup is corrupt or unreadable; existing files were retained"
                        ) from None
        except asyncio.TimeoutError:
            raise ValueError(
                "Stop wrapper sessions before restoring metadata"
            ) from None
    try:
        db = sqlite3.connect(state / "pool.sqlite")
        checks["database"] = db.execute("PRAGMA quick_check").fetchone()[0]
        db.close()
    except sqlite3.Error:
        checks["database"] = "corrupt or unreadable"
    checks["state_permissions"] = oct(state.stat().st_mode & 0o777)
    try:
        async with file_lock(state / "compatibility.lock"):
            checks["compatibility"] = await asyncio.to_thread(
                compatibility.check, binary, state
            )
    except Exception:
        checks["compatibility"] = "failed; no credentials were read"
    checks["keychain_reader"] = Path("/usr/bin/security").exists()
    try:
        import broker

        checks["broker"] = await broker.exchange(state, {"op": "ping"}, 2)
    except Exception:
        checks["broker"] = "not running (starts on demand)"
    if args.self_test:
        source = Path(__file__).parent
        tests = (
            source / "tests" if (source / "tests").is_dir() else source.parent / "tests"
        )
        if not tests.is_dir():
            raise ValueError(
                "Offline checks are missing. Reinstall Relay and try again."
            )
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(source), str(tests), os.environ.get("PYTHONPATH", "")]
            ),
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(tests),
            "-q",
            cwd=str(tests),
            env=env,
        )
        checks["offline_tests_exit_code"] = await proc.wait()
    if args.backup and checks["database"] == "ok":
        target = state / "pool.backup.sqlite"
        source = sqlite3.connect(state / "pool.sqlite")
        dest = sqlite3.connect(target)
        source.backup(dest)
        source.close()
        dest.close()
        target.chmod(0o600)
        checks["backup"] = str(target)
    healthy = (
        checks.get("database") == "ok"
        and isinstance(checks.get("compatibility"), dict)
        and checks.get("offline_tests_exit_code", 0) == 0
    )
    print(json.dumps(result, indent=2))
    return 0 if healthy else 1


async def group(args, state, binary):
    from router import RouterError
    import service

    path = state / "groups.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    if args.action == "list":
        print(json.dumps(data, indent=2))
        return 0
    if args.action == "save":
        data[args.name] = [
            {"name": Path(p).name, "cwd": str(Path(p).expanduser().resolve())}
            for p in args.paths
        ]
        settings.atomic_json(path, data)
        print("Saved group " + args.name)
        return 0
    if args.action == "remove":
        data.pop(args.name, None)
        settings.atomic_json(path, data)
        return 0
    if args.name not in data:
        raise RouterError("Unknown task group")
    if args.action == "start":
        import runtime

        async with file_lock(state / "compatibility.lock"):
            binary, _, _ = await asyncio.to_thread(runtime.select, binary, state)
        for spec in data[args.name]:
            ident = service.start(
                state,
                name=args.name + "/" + spec["name"],
                cwd=spec["cwd"],
                arguments=["--yolo"] if args.yolo else [],
                binary=binary,
            )
            print(ident + " " + spec["name"])
    return 0


def add_parsers(sub):
    preview = sub.add_parser(
        "explain", help="Preview a native Codex launch without starting a task"
    )
    preview.add_argument("codex_args", nargs="...")
    diagnostics = sub.add_parser(
        "diagnostics",
        help="Export redacted operational facts without credentials, prompts, or project paths",
    )
    diagnostics.add_argument("output")
    clean = sub.add_parser(
        "clean", help="Preview safe cleanup of unreferenced old runtime files"
    )
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--older-than", type=int, default=30, dest="days")
    clean.add_argument(
        "--jobs",
        action="store_true",
        help="Also remove old stopped jobs; saved workspace references are retained",
    )
    workspaces = sub.add_parser(
        "workspace",
        help="Save and restore the same conversations across several terminals",
    )
    ws = workspaces.add_subparsers(dest="action", required=True)
    ws.add_parser("list")
    save_ws = ws.add_parser("save")
    save_ws.add_argument("name")
    save_ws.add_argument("sessions", nargs="*")
    for action in ("restore", "remove"):
        child = ws.add_parser(action)
        child.add_argument("name")
    status = sub.add_parser(
        "status", help="Account and session dashboard without live network requests"
    )
    status.add_argument("--json", action="store_true")
    status.add_argument("--watch", action="store_true")
    status.add_argument(
        "--all",
        action="store_true",
        help="Include stopped and completed saved sessions",
    )
    restart = sub.add_parser(
        "restart", help="Restart a stopped session from saved history"
    )
    restart.add_argument("session")
    restart.add_argument("--if-stopped", action="store_true")
    status.add_argument(
        "--interval",
        type=float,
        default=30,
        help="Maximum local filesystem wait; output changes only when data changes",
    )
    for command in ["pause", "unpause", "retry", "wake", "stop", "switch"]:
        p = sub.add_parser(command, help=command + " one live session or all")
        p.add_argument("target", nargs="?")
        p.add_argument("--thread")
        p.add_argument(
            "--wait",
            action="store_true",
            help="Wait for the worker to confirm the action",
        )
        p.add_argument("--timeout", type=float, default=15)
        if command == "switch":
            p.add_argument("--account", required=True)
    account = sub.add_parser("account", help="Manage any number of enrolled accounts")
    child = account.add_subparsers(dest="action", required=True)
    child.add_parser("identify", help="Fill in account emails from saved local logins")
    for action in [
        "add",
        "reauth",
        "remove",
        "enable",
        "disable",
        "rename",
        "priority",
        "cap",
        "models",
    ]:
        p = child.add_parser(action)
        p.add_argument("label")
        if action in {"add", "reauth"}:
            p.add_argument("--device-auth", action="store_true")
        if action == "remove":
            p.add_argument("--revoke", action="store_true")
        if action == "rename":
            p.add_argument("value")
        if action in {"priority", "cap"}:
            p.add_argument("value", type=int)
        if action == "models":
            p.add_argument("values", nargs="*")
    config = sub.add_parser("config", help="Set global or named recovery profiles")
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?")
    config.add_argument("--profile")
    logs = sub.add_parser("events", help="Read bounded metadata-only event history")
    logs.add_argument("--limit", type=int, default=30)
    logs.add_argument("--json", action="store_true")
    hist = sub.add_parser("usage", help="Last observed allowance and local estimates")
    hist.add_argument("--account")
    hist.add_argument("--json", action="store_true")
    doc = sub.add_parser(
        "doctor", help="Check installation, database, broker and compatibility"
    )
    doc.add_argument("--self-test", action="store_true")
    doc.add_argument("--backup", action="store_true")
    doc.add_argument(
        "--restore-backup",
        action="store_true",
        help="Restore metadata when wrapper sessions are stopped; retain the replaced database",
    )
    runtime = sub.add_parser(
        "runtime", help="Preflight, snapshot or select a tested native Codex runtime"
    )
    runtime.add_argument("action", choices=["list", "cache", "use", "preflight"])
    runtime.add_argument("value", nargs="?")
    startup = sub.add_parser(
        "startup", help="Choose background sessions to recover at Mac login"
    )
    startup.add_argument("action", choices=["list", "enable", "disable"])
    startup.add_argument("session", nargs="?")
    menu = sub.add_parser(
        "menubar", help="Build or launch the optional native menu app"
    )
    menu.add_argument("--build-only", action="store_true")
    menu.add_argument(
        "--no-startup", action="store_true", help="Disable menu login startup"
    )
    menu.add_argument(
        "--configure-only", action="store_true", help="Only update the login setting"
    )
    start = sub.add_parser(
        "start", help="Start a supervised background task; attach later"
    )
    for flag in ["name", "cwd", "first", "resume", "prompt", "profile"]:
        start.add_argument("--" + flag)
    start.add_argument("codex_args", nargs="...")
    attach = sub.add_parser(
        "attach", help="Attach the native terminal to a background session"
    )
    attach.add_argument("session")
    attach.add_argument("codex_args", nargs="...")
    attach.add_argument(
        "--takeover",
        action="store_true",
        help="Move an existing terminal attachment without stopping its task",
    )
    groups = sub.add_parser("group", help="Save and launch groups of project tasks")
    g = groups.add_subparsers(dest="action", required=True)
    g.add_parser("list")
    save = g.add_parser("save")
    save.add_argument("name")
    save.add_argument("paths", nargs="+")
    rm = g.add_parser("remove")
    rm.add_argument("name")
    launch = g.add_parser("start")
    launch.add_argument("name")
    launch.add_argument("--yolo", action="store_true")
    for command in ["_supervise", "_serve"]:
        p = sub.add_parser(command, help=__import__("argparse").SUPPRESS)
        p.add_argument("session")
    b = sub.add_parser("_broker", help=__import__("argparse").SUPPRESS)
    b.add_argument("--binary")


async def dispatch(args, state, binary):
    from router import RouterError

    if args.command in {"explain", "diagnostics", "clean", "workspace"}:
        import tools

        if args.command == "explain":
            result = tools.explain(args.codex_args, os.getcwd(), binary)
        elif args.command == "diagnostics":
            result = {"written": tools.diagnostics(state, args.output)}
        elif args.command == "clean":
            if args.days < 1:
                raise ValueError("Retention must be at least one day")
            from pool import file_lock

            async with file_lock(state / "cleanup.lock"):
                if args.apply:
                    try:
                        async with asyncio.timeout(2):
                            async with file_lock(state / "metadata.lock"):
                                result = tools.cleanup(
                                    state, apply=True, days=args.days, jobs=args.jobs
                                )
                    except asyncio.TimeoutError:
                        raise RouterError(
                            "Stop supervised sessions before applying cleanup. The dry run works while sessions are active."
                        ) from None
                else:
                    result = tools.cleanup(
                        state, apply=args.apply, days=args.days, jobs=args.jobs
                    )
        else:
            result = tools.workspace(
                state,
                args.action,
                getattr(args, "name", None),
                getattr(args, "sessions", None),
                binary,
            )
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "startup":
        import desktop

        if args.action != "list" and not args.session:
            raise ValueError("Choose a saved session ID")
        print(
            json.dumps(desktop.job_startup(state, args.action, args.session), indent=2)
        )
        return 0
    if args.command == "restart":
        import service

        if args.if_stopped:
            pool = Pool(state)
            try:
                ident = service.resolve(state, args.session)
                if any(s["id"] == ident for s in pool.detail()):
                    return 0
            finally:
                pool.close()

        async with file_lock(state / "restart.lock"):
            ident = service.restart(state, args.session)
        print("Restarted " + ident)
        return 0
    if args.command == "status":
        if args.interval < 0.5:
            raise RouterError("Watch interval must be at least 0.5 seconds")
        return await status(args, state)
    if args.command in {"pause", "unpause", "retry", "wake", "stop", "switch"}:
        return await control(args, state)
    if args.command == "account":
        if args.action == "identify":
            print(
                f"Identified {await asyncio.to_thread(identify_accounts, state)} saved accounts."
            )
            return 0
        return await manage(args, state, binary)
    if args.command == "config":
        if args.key:
            if args.value is None:
                raise RouterError("Provide a setting value")
            settings.edit(state, args.key, args.value, args.profile)
            pool = Pool(state)
            pool.signal()
            pool.close()
        print(json.dumps(settings.load(state, os.getcwd(), args.profile), indent=2))
        return 0
    if args.command == "events":
        pool = Pool(state)
        try:
            with pool.db() as db:
                rows = db.execute(
                    "SELECT at,owner,kind,details FROM events ORDER BY id DESC LIMIT ?",
                    (max(1, min(args.limit, 2000)),),
                ).fetchall()
            value = [
                dict(at=r[0], session=r[1], kind=r[2], details=json.loads(r[3]))
                for r in rows
            ]
            print(
                json.dumps(value, indent=2)
                if args.json
                else "\n".join(
                    time.strftime("%m-%d %H:%M:%S", time.localtime(v["at"]))
                    + " "
                    + v["session"][:10]
                    + " "
                    + v["kind"]
                    + " "
                    + json.dumps(v["details"])
                    for v in value
                )
            )
            return 0
        finally:
            pool.close()
    if args.command == "usage":
        label = resolve_label(state, args.account) if args.account else None
        print(json.dumps(history(state, label), indent=2))
        return 0
    if args.command == "doctor":
        return await doctor(args, state, binary)
    if args.command == "runtime":
        import runtime, compatibility

        async with file_lock(state / "compatibility.lock"):
            if args.action == "list":
                print(json.dumps(runtime.index(state), indent=2))
            elif args.action == "use":
                runtime.choose(state, args.value)
                print(
                    "Wrapper runtime selection updated; existing sessions are unchanged."
                )
            elif args.action == "cache":
                path, result = await asyncio.to_thread(
                    runtime.capture, args.value or binary, state
                )
                print(result["version"] + " cached at " + path)
            else:
                result = await asyncio.to_thread(
                    compatibility.check, args.value or binary, state
                )
                print(json.dumps(result, indent=2))
        return 0
    if args.command == "menubar":
        import desktop

        if args.no_startup or args.configure_only:
            desktop.login_item(state, enabled=not args.no_startup)
            if args.configure_only:
                print(
                    "Menu login startup "
                    + ("disabled" if args.no_startup else "enabled")
                )
                return 0
        app = await asyncio.to_thread(
            desktop.menubar, state, None, args.build_only, not args.no_startup
        )
        print(str(app))
        return 0
    if args.command == "group":
        return await group(args, state, binary)
    if args.command == "start":
        import service

        first = resolve_label(state, args.first) if args.first else None
        ident = service.start(
            state,
            name=args.name,
            cwd=args.cwd,
            first=first,
            arguments=args.codex_args,
            resume=args.resume,
            prompt=args.prompt,
            profile=args.profile,
            binary=binary,
        )
        print("Started " + ident + "\nAttach: relay attach " + ident[:10])
        return 0
    if args.command == "attach":
        import service

        return await service.attach(
            state, args.session, binary, args.codex_args or None, takeover=args.takeover
        )
    return None
