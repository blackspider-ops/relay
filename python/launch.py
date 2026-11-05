"""Keep native argument boundaries while adapting interactive remote launches."""

import json
import functools
import subprocess
import os
import re
import tomllib
from pathlib import Path

# Values must be consumed together: a value resembling another flag is still a
# value. Unknown options remain untouched for the installed native CLI to validate.
RESUME_FIELDS = {
    "model",
    "modelProvider",
    "cwd",
    "approvalPolicy",
    "approvalsReviewer",
    "sandbox",
    "permissions",
    "serviceTier",
    "runtimeWorkspaceRoots",
    "config",
    "personality",
    "baseInstructions",
    "developerInstructions",
}


VALUE_FLAGS = {
    "-c": "--config",
    "-p": "--profile",
    "-m": "--model",
    "-C": "--cd",
    "-i": "--image",
    "-s": "--sandbox",
    "-a": "--ask-for-approval",
    "-P": "--permission-profile",
    "--enable": "--enable",
    "--disable": "--disable",
    "--local-provider": "--local-provider",
    "--add-dir": "--add-dir",
    "--remote": "--remote",
    "--remote-auth-token-env": "--remote-auth-token-env",
    "--thread-source": "--thread-source",
    "--cyber-access-program": "--cyber-access-program",
    "--output-schema": "--output-schema",
    "--color": "--color",
    "-o": "--output-last-message",
    "--base": "--base",
    "--commit": "--commit",
    "--title": "--title",
}
VALUE_FLAGS.update({value: value for value in list(VALUE_FLAGS.values())})


def arguments_only(arguments):
    arguments = list(arguments)
    return arguments[1:] if arguments[:1] == ["--"] else arguments


def tokens(arguments):
    """Yield (canonical option, value, original tokens); stop parsing at --."""
    arguments = list(arguments)
    i = 0
    while i < len(arguments):
        arg = arguments[i]
        if arg == "--":
            yield None, None, arguments[i:]
            return
        name, sep, value = arg.partition("=")
        if name in VALUE_FLAGS:
            if sep:
                yield VALUE_FLAGS[name], value, [arg]
            else:
                if i + 1 == len(arguments):
                    raise ValueError(f"{arg} requires a value")
                yield VALUE_FLAGS[name], arguments[i + 1], arguments[i : i + 2]
                i += 1
        elif len(arg) > 2 and arg[:2] in VALUE_FLAGS and not arg.startswith("--"):
            yield VALUE_FLAGS[arg[:2]], arg[2:].removeprefix("="), [arg]
        else:
            yield arg if arg.startswith("-") else None, arg, [arg]
        i += 1


def working_directory(arguments, cwd):
    target = Path(cwd)
    for name, value, _ in tokens(arguments_only(arguments)):
        if name == "--cd":
            path = Path(value).expanduser()
            target = path if path.is_absolute() else Path(cwd) / path
    return str(target.resolve())


def frontend_arguments(arguments, cwd):
    """Remote Codex needs an explicit cwd to offer its Cwd session filter."""
    arguments = arguments_only(arguments)
    target = working_directory(arguments, cwd)
    result = ["-C", target]
    for name, _, original in tokens(arguments):
        if name != "--cd":
            result.extend(original)
    return result


def translate(arguments):
    frontend, overrides = [], {}
    for name, value, original in tokens(arguments_only(arguments)):
        if name in ("--yolo", "--dangerously-bypass-approvals-and-sandbox"):
            overrides.update(approvalPolicy="never", sandbox="danger-full-access")
        elif name == "--approve-for-me":
            overrides.update(
                approvalPolicy="on-request",
                sandbox="workspace-write",
                approvalsReviewer="auto_review",
            )
        elif name in ("--sandbox", "--ask-for-approval", "--permission-profile"):
            overrides[
                {
                    "--sandbox": "sandbox",
                    "--ask-for-approval": "approvalPolicy",
                    "--permission-profile": "permissions",
                }[name]
            ] = value
        elif name not in {"--no-daemon", "--profile", "--add-dir"}:
            # Relay already gives each managed task its own private app server.
            frontend.extend(original)
    if "sandbox" in overrides and "permissions" in overrides:
        raise ValueError(
            "Choose either --sandbox/--yolo or a named permission profile."
        )
    if overrides.get("sandbox") not in (
        None,
        "read-only",
        "workspace-write",
        "danger-full-access",
    ):
        raise ValueError("Unsupported sandbox mode.")
    if overrides.get("approvalPolicy") not in (
        None,
        "never",
        "on-request",
        "untrusted",
        "on-failure",
    ):
        raise ValueError("Unsupported approval policy.")
    return frontend, overrides


def backend_arguments(arguments):
    """Mirror server config without reading prompts or other options' values."""
    result = []
    parsed = list(tokens(arguments_only(arguments)))
    for name, value, _ in parsed:
        if name == "--profile":
            result.extend(profile_arguments(value))
    for name, value, original in parsed:
        if name in {"--config", "--enable", "--disable"}:
            result.extend(original)
        elif name == "--model":
            result.extend(["-c", "model=" + json.dumps(value)])
        elif name == "--search":
            result.extend(["-c", 'web_search="live"'])
        elif name == "--strict-config":
            result.append(name)
    return result


NATIVE_COMMANDS = {
    "agents",
    "login",
    "logout",
    "mcp",
    "plugin",
    "app-server",
    "remote-control",
    "app",
    "completion",
    "update",
    "doctor",
    "sandbox",
    "debug",
    "apply",
    "a",
    "queue",
    "archive",
    "delete",
    "migrate-rollouts",
    "migrate-nfs-db",
    "unarchive",
    "cloud",
    "exec-server",
    "features",
    "help",
}


@functools.lru_cache(maxsize=8)
def native_commands(binary):
    result = subprocess.run(
        [binary, "--help"], capture_output=True, text=True, timeout=15
    )
    if result.returncode:
        return NATIVE_COMMANDS
    section = result.stdout.split("Commands:", 1)
    commands = set(NATIVE_COMMANDS)
    if len(section) > 1:
        for line in section[1].splitlines():
            if line and not line.startswith(" "):
                break
            match = re.match(r"  ([a-z][a-z0-9-]*)\s", line)
            if match:
                commands.add(match.group(1))
    return commands - {"exec", "e", "review", "resume", "fork"}


def native_mode(arguments, binary=None):
    """Modes outside the remote interactive protocol run native, unchanged."""
    commands = native_commands(binary) if binary else NATIVE_COMMANDS
    first_positional = True
    initial_command = None
    for name, value, original in tokens(arguments_only(arguments)):
        if name in {
            "--help",
            "-h",
            "--version",
            "-V",
            "--remote",
            "--oss",
            "--local-provider",
            "--ignore-user-config",
            "--ignore-rules",
            "--dangerously-bypass-hook-trust",
        }:
            return True
        if name is None and len(original) == 1 and first_positional:
            first_positional = False
            initial_command = value
            if value in commands:
                return True
        elif name is None and value == "help" and initial_command in {"exec", "e"}:
            return True
    return False


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(json.dumps(k) + "=" + toml_value(v) for k, v in value.items())
            + "}"
        )
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise ValueError("Unsupported profile value")


def profile_arguments(name, home=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(
            "Choose a native profile name containing letters, numbers, - or _"
        )
    home = Path(home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    path = home / (name + ".config.toml")
    if not path.is_file():
        raise ValueError("Native Codex profile does not exist: " + name)
    data = tomllib.loads(path.read_text())
    result = []

    def flatten(value, keys):
        if any(
            re.search(r"token|secret|password|api_key|authorization|headers", key, re.I)
            for key in keys
        ):
            return
        if isinstance(value, dict) and value:
            if all(re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in value):
                for key, child in value.items():
                    flatten(child, [*keys, key])
                return
            if sensitive_config(value):
                return
        if isinstance(value, list) and sensitive_config(value):
            return
        result.extend(["-c", ".".join(keys) + "=" + toml_value(value)])

    for key, value in data.items():
        flatten(value, [key])
    return result


def additional_roots(arguments, cwd):
    roots = []
    for name, value, _ in tokens(arguments_only(arguments)):
        if name == "--add-dir":
            path = Path(value).expanduser()
            roots.append(
                str((path if path.is_absolute() else Path(cwd) / path).resolve())
            )
    return roots


def prepare_worktree(arguments, state, cwd):
    """Create an isolated Git worktree once; never remove a user's working files."""
    import subprocess
    import uuid

    parsed = list(tokens(arguments_only(arguments)))
    if not any(name == "--worktree" for name, _, _ in parsed):
        return list(arguments), cwd
    positional = [
        value for name, value, raw in parsed if name is None and len(raw) == 1
    ]
    flags = {name for name, _, _ in parsed}
    if (
        "resume" in positional[:2]
        or "review" in positional[:2]
        or flags & {"--ephemeral", "--ignore-user-config"}
    ):
        raise ValueError(
            "--worktree cannot be combined with resume, review, --ephemeral, or --ignore-user-config"
        )
    if "fork" in positional[:2] and (
        "--last" in flags or len(positional) < positional.index("fork") + 2
    ):
        raise ValueError("A worktree fork requires an explicit session ID")
    probe = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if probe.returncode:
        raise ValueError("--worktree requires a Git repository with a commit")
    root = state / "worktrees"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / uuid.uuid4().hex[:12]
    create = subprocess.run(
        ["git", "-C", cwd, "worktree", "add", "--detach", str(target), "HEAD"],
        capture_output=True,
        text=True,
    )
    if create.returncode:
        raise ValueError(
            "Git could not create a worktree. Check the repository and available disk space."
        )
    result = []
    for name, _, raw in parsed:
        if name != "--worktree":
            result.extend(raw)
    result = frontend_arguments(result, cwd)
    result[result.index("-C") + 1] = str(target.resolve())
    return result, str(target.resolve())


def sensitive_config(value):
    if isinstance(value, dict):
        return any(
            re.search(r"token|secret|password|api_key|authorization|headers", key, re.I)
            or sensitive_config(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(sensitive_config(child) for child in value)
    return False


def profile_config(arguments, home=None):
    """Secret-bearing profile fields travel over private RPC, not process argv."""
    home = Path(home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    result = {}
    for name, value, _ in tokens(arguments_only(arguments)):
        if name == "--profile":
            # Validate names and existence through the same path as backend arguments.
            profile_arguments(value, home)
            data = tomllib.loads((home / (value + ".config.toml")).read_text())

            def contains_sensitive(child, keys=()):
                if any(
                    re.search(
                        r"token|secret|password|api_key|authorization|headers",
                        key,
                        re.I,
                    )
                    for key in keys
                ):
                    return True
                if isinstance(child, dict):
                    return any(
                        contains_sensitive(v, (*keys, k)) for k, v in child.items()
                    )
                if isinstance(child, list):
                    return any(contains_sensitive(v, keys) for v in child)
                return False

            for key, child in data.items():
                if contains_sensitive(child, (key,)):
                    result[key] = child
    # Explicit native overrides retain higher precedence than a profile layer.
    for name, value, _ in tokens(arguments_only(arguments)):
        if name == "--config" and "=" in value:
            key, raw = value.split("=", 1)
            if key.split(".")[0] in result:
                try:
                    parsed = tomllib.loads("value=" + raw)["value"]
                except tomllib.TOMLDecodeError:
                    parsed = raw
                result[key] = parsed
    return result
