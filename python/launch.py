"""Translate local launch preferences into explicit app-server thread settings."""


def translate(arguments):
    arguments = list(arguments)
    if arguments and arguments[0] == "--":
        arguments.pop(0)
    frontend, overrides = [], {}
    i = 0
    while i < len(arguments):
        arg = arguments[i]
        if arg == "--":
            frontend.extend(arguments[i:])
            break
        if arg in ("--yolo", "--dangerously-bypass-approvals-and-sandbox"):
            overrides.update(approvalPolicy="never", sandbox="danger-full-access")
        elif arg in (
            "-s",
            "--sandbox",
            "-a",
            "--ask-for-approval",
            "-P",
            "--permission-profile",
        ):
            if i + 1 == len(arguments):
                raise ValueError(f"{arg} requires a value")
            i += 1
            key = {
                "-s": "sandbox",
                "--sandbox": "sandbox",
                "-a": "approvalPolicy",
                "--ask-for-approval": "approvalPolicy",
                "-P": "permissions",
                "--permission-profile": "permissions",
            }[arg]
            overrides[key] = arguments[i]
        elif any(
            arg.startswith(flag + "=")
            for flag in ("--sandbox", "--ask-for-approval", "--permission-profile")
        ):
            name, value = arg.split("=", 1)
            overrides[
                {
                    "--sandbox": "sandbox",
                    "--ask-for-approval": "approvalPolicy",
                    "--permission-profile": "permissions",
                }[name]
            ] = value
        else:
            frontend.append(arg)
        i += 1
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
    """Copy config/profile selection to the backend without consuming native UI args."""
    result = []
    i = 0
    while i < len(arguments):
        arg = arguments[i]
        if arg in {
            "-c",
            "--config",
            "-p",
            "--profile",
            "--enable",
            "--disable",
            "-m",
            "--model",
        } and i + 1 < len(arguments):
            result.extend([arg, arguments[i + 1]])
            i += 2
            continue
        if arg.startswith(
            ("--config=", "--profile=", "--enable=", "--disable=", "--model=")
        ):
            result.append(arg)
        elif arg == "--search":
            result.extend(["-c", 'web_search="live"'])
        i += 1
    # app-server has no --model flag; a model config override has the same default.
    converted = []
    i = 0
    while i < len(result):
        if result[i] in {"-m", "--model"}:
            import json

            converted.extend(["-c", "model=" + json.dumps(result[i + 1])])
            i += 2
        elif result[i].startswith("--model="):
            import json

            converted.extend(["-c", "model=" + json.dumps(result[i].split("=", 1)[1])])
            i += 1
        else:
            converted.append(result[i])
            i += 1
    return converted
