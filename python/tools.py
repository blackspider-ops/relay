"""Local launch explanations, redacted diagnostics, saved workspaces, and cleanup."""

import json
import os
from pathlib import Path
import shutil
import sys
import time

from settings import atomic_json
from pool import Pool


def explain(arguments, cwd, binary=None):
    from launch import (
        translate,
        working_directory,
        native_mode,
        additional_roots,
        tokens,
        arguments_only,
    )
    from router import VERSION

    if binary is None:
        from router import codex_path

        try:
            binary = codex_path()
        except Exception:
            binary = None
    native = native_mode(arguments, binary)
    _, overrides = translate(arguments)
    model = next(
        (
            value
            for name, value, _ in tokens(arguments_only(arguments))
            if name == "--model"
        ),
        None,
    )
    return {
        "relay_version": VERSION,
        "mode": "native" if native else "managed",
        "account_recovery": not native,
        "cwd": working_directory(arguments, cwd),
        "model": model or "Native Codex default",
        "permissions": overrides or "Saved thread or native Codex defaults",
        "additional_directories": additional_roots(
            arguments, working_directory(arguments, cwd)
        ),
        "codex": binary,
        "note": "Native utilities and external/local-provider endpoints retain native behavior."
        if native
        else "Relay manages account resets, temporary failures, and the saved conversation.",
    }


def diagnostics(state, output):
    """Allowlisted operational facts only; never read credentials or raw job arguments."""
    from router import VERSION
    import platform

    pool = Pool(state)
    try:
        details = pool.detail()
        with pool.db() as db:
            health = db.execute("PRAGMA quick_check").fetchone()[0]
            events = [
                r[0]
                for r in db.execute(
                    "SELECT kind FROM events ORDER BY id DESC LIMIT 100"
                )
            ]
        value = {
            "relay_version": VERSION,
            "python": platform.python_version(),
            "platform": sys.platform,
            "database": "ok" if health == "ok" else "failed",
            "active_sessions": len(details),
            "session_states": [r.get("state", "unknown") for r in details],
            "recent_event_types": events,
        }
        destination = Path(output).expanduser()
        with destination.open("x", encoding="utf-8") as stream:
            os.chmod(destination, 0o600)
            json.dump(value, stream, indent=2)
            stream.write("\n")
        return str(destination)
    finally:
        pool.close()


def workspace(state, action, name=None, sessions=None, binary=None):
    import service

    path = state / "workspaces.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    pool = Pool(state)
    try:
        if action == "list":
            return {key: len(value) for key, value in data.items()}
        if action == "remove":
            data.pop(name, None)
            atomic_json(path, data)
            return {"removed": name}
        if action == "save":
            live = pool.detail()
            chosen = [
                r
                for r in live
                if not sessions or any(r["id"].startswith(s) for s in sessions)
            ]
            if not chosen or sessions and len(chosen) != len(sessions):
                raise ValueError(
                    "Choose live session IDs from relay status, or omit IDs to save every live session"
                )
            jobs = []
            for row in chosen:
                job_path = service.folder(state) / (row["id"] + ".job.json")
                if not job_path.exists():
                    continue
                jobs.append(row["id"])
            if not jobs:
                raise ValueError("No supervised sessions were available to save")
            data[name] = jobs
            atomic_json(path, data)
            return {"saved": name, "sessions": len(jobs)}
        if name not in data:
            raise ValueError("Unknown workspace")
        alive = {r["id"] for r in pool.detail()}
        result = []
        for ident in data[name]:
            if ident not in alive:
                service.restart(state, ident)
            result.append({"session": ident, "attach": "relay attach " + ident})
        return result
    finally:
        pool.close()


def cleanup(state, *, apply=False, days=30, jobs=False):
    """Default to a dry run. Preserve runtimes used by live or restartable jobs."""
    import runtime

    pool = Pool(state)
    try:
        live = {r["id"] for r in pool.detail()}
    finally:
        pool.close()
    threshold = time.time() - days * 86400
    protected = {str(Path(__file__).parent.resolve())}
    workspaces_file = state / "workspaces.json"
    saved_workspaces = (
        json.loads(workspaces_file.read_text()) if workspaces_file.exists() else {}
    )
    workspace_jobs = {ident for group in saved_workspaces.values() for ident in group}
    value = runtime.index(state)
    native_protected = {
        row["sha256"][:20] for row in value["runtimes"][-1:] if row.get("sha256")
    }
    if value.get("selected"):
        native_protected.add(value["selected"][:20])
    candidates = []
    protect_all_python = False
    for path in (state / "services").glob("*.job.json"):
        try:
            job = json.loads(path.read_text())
        except (OSError, ValueError):
            protect_all_python = True
            continue
        ident = path.name.removesuffix(".job.json")
        old_stopped = (
            ident not in live
            and job.get("state") in {"stopped", "removed"}
            and path.stat().st_mtime < threshold
        )
        if old_stopped and jobs and ident not in workspace_jobs:
            candidates.extend(
                p
                for p in path.parent.glob(ident + ".*")
                if p.is_file() and not p.is_symlink()
            )
            continue
        if job.get("binary"):
            protected.add(str(Path(job["binary"]).resolve()))
        if job.get("relay_runtime"):
            protected.add(str(Path(job["relay_runtime"]).resolve()))
        elif ident in live:
            protect_all_python = True
    for folder in ["relay-runtimes", "native-runtimes"]:
        root = state / folder
        if not root.exists() or folder == "relay-runtimes" and protect_all_python:
            continue
        for path in root.iterdir():
            resolved = str(path.resolve())
            if (
                path.is_symlink()
                or not path.is_dir()
                or path.stat().st_mtime >= threshold
            ):
                continue
            if folder == "native-runtimes" and path.name in native_protected:
                continue
            if any(p == resolved or p.startswith(resolved + os.sep) for p in protected):
                continue
            candidates.append(path)
    # Recheck live references immediately before mutations. Never delete from another user's state.
    if apply:
        guard = Pool(state)
        try:
            if {r["id"] for r in guard.detail()} != live:
                raise ValueError("Sessions changed during cleanup; rerun the dry run")
        finally:
            guard.close()
        for path in candidates:
            if path.is_symlink():
                raise ValueError(
                    "Cleanup encountered a changed symlink; nothing further was removed"
                )
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        value["runtimes"] = [
            r for r in value["runtimes"] if Path(r["binary"]).is_file()
        ]
        atomic_json(state / "runtimes.json", value)
    return {
        "dry_run": not apply,
        "paths": [str(p) for p in candidates],
        "count": len(candidates),
    }
