"""Versioned native runtime snapshots; never replace the user's Codex installation."""

import json
import shutil
import time

import compatibility
from settings import atomic_json


def index(state):
    file = state / "runtimes.json"
    return (
        json.loads(file.read_text())
        if file.exists()
        else {"runtimes": [], "selected": None}
    )


def capture(binary, state):
    verified = compatibility.check(binary, state)
    native = compatibility.executable(binary)
    digest = compatibility.digest(native)
    root = state / "native-runtimes" / digest[:20]
    target = root / "vendor" / "bin" / "codex"
    if not target.exists():
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = root / "staging"
        if temporary.exists():
            shutil.rmtree(temporary)
        if native.parent.name == "bin":
            shutil.copytree(native.parent.parent, temporary)
        else:
            (temporary / "bin").mkdir(parents=True)
            shutil.copy2(native, temporary / "bin" / "codex")
        temporary.rename(root / "vendor")
    if compatibility.digest(target) != digest:
        raise ValueError("Cached runtime integrity check failed")
    value = index(state)
    if not any(row["sha256"] == digest for row in value["runtimes"]):
        value["runtimes"].append(
            {
                "version": verified["version"],
                "sha256": digest,
                "binary": str(target),
                "captured": time.time(),
            }
        )
        atomic_json(state / "runtimes.json", value)
    return str(target), verified


def select(binary, state):
    value = index(state)
    if value.get("selected"):
        choices = [r for r in value["runtimes"] if r["sha256"] == value["selected"]]
        if not choices:
            raise ValueError("Selected runtime snapshot is missing")
        selected = choices[0]
        if compatibility.digest(selected["binary"]) != selected["sha256"]:
            raise ValueError("Selected runtime snapshot was modified")
        return (
            selected["binary"],
            compatibility.check(selected["binary"], state),
            "selected_snapshot",
        )
    try:
        path, verified = capture(binary, state)
        return path, verified, "installed_snapshot"
    except Exception as error:
        for row in reversed(value["runtimes"]):
            try:
                if compatibility.digest(row["binary"]) != row["sha256"]:
                    continue
                verified = compatibility.check(row["binary"], state)
                return row["binary"], verified, "compatible_fallback"
            except Exception:
                continue
        raise error


def choose(state, prefix=None):
    value = index(state)
    if prefix in (None, "latest", "installed"):
        value["selected"] = None
    else:
        matching = [
            row
            for row in value["runtimes"]
            if row["sha256"].startswith(prefix) or row["version"] == prefix
        ]
        if len(matching) != 1:
            raise ValueError(
                "Choose a unique runtime hash or version from runtime list"
            )
        value["selected"] = matching[0]["sha256"]
    atomic_json(state / "runtimes.json", value)
