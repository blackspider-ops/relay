"""Validated local preferences; no automatic model fallback or extra billing."""

from pathlib import Path
import copy
import json
import os
import tomllib

DEFAULTS = {
    "retry_interval": 300,
    "retry_attempts": 3,
    "unknown_reset_interval": 300,
    "watchdog_interval": 30,
    "idle_warning_after": 120,
    "routing": "balanced",
    "notifications": False,
    "fallback_models": [],
    "preferred_accounts": [],
    "max_sessions_per_account": 0,
    "reservation_seconds": 120,
    "history_days": 30,
    "event_limit": 2000,
    "offline_retry": 30,
    "background_on_disconnect": False,
    "broker_idle_seconds": 600,
    "keep_awake": False,
}


def validate(values):
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
    for key, value in values.items():
        expected = DEFAULTS[key]
        if isinstance(expected, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        elif isinstance(expected, int):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
            if key not in {"max_sessions_per_account", "retry_attempts"} and value < 1:
                raise ValueError(f"{key} must be positive")
        elif isinstance(expected, list):
            if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value
            ):
                raise ValueError(f"{key} must be a list of strings")
    if values.get("routing", "balanced") not in {
        "balanced",
        "quota",
        "priority",
        "round-robin",
    }:
        raise ValueError("routing must be balanced, quota, priority, or round-robin")
    if values.get("reservation_seconds", 120) < 30:
        raise ValueError(
            "reservation_seconds must be at least 30 to allow heartbeat renewal"
        )
    return values


def load(state, project=None, profile=None):
    result = copy.deepcopy(DEFAULTS)
    for path in [
        state / "settings.json",
        Path(project) / ".codex-accounts.toml" if project else None,
        Path(project) / ".relay.toml" if project else None,
    ]:
        if not path or not path.exists():
            continue
        data = (
            json.loads(path.read_text())
            if path.suffix == ".json"
            else tomllib.loads(path.read_text())
        )
        result.update(validate(data.get("settings", {})))
        if profile:
            result.update(validate(data.get("profiles", {}).get(profile, {})))
    return result


def edit(state, key, raw, profile=None):
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    validate({key: value})
    path = state / "settings.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    target = (
        data.setdefault("profiles", {}).setdefault(profile, {})
        if profile
        else data.setdefault("settings", {})
    )
    target[key] = value
    atomic_json(path, data)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
