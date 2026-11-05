"""Keep native argument boundaries while adapting interactive remote launches."""

import json
from pathlib import Path

# Values must be consumed together: a value resembling another flag is still a
# value. Unknown options remain untouched for the installed native CLI to validate.
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
        elif name != "--no-daemon":
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
    for name, value, original in tokens(arguments_only(arguments)):
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
    "exec",
    "e",
    "review",
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


def native_mode(arguments):
    """Modes outside the remote interactive protocol run native, unchanged."""
    first_positional = True
    for name, value, original in tokens(arguments_only(arguments)):
        if name in {
            "--help",
            "-h",
            "--version",
            "-V",
            "--remote",
            "--oss",
            "--local-provider",
            "--profile",
            "--add-dir",
            "--worktree",
            "--dangerously-bypass-hook-trust",
        }:
            return True
        if name is None and len(original) == 1 and first_positional:
            first_positional = False
            if value in NATIVE_COMMANDS:
                return True
    return False
