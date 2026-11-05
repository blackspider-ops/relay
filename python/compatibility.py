"""Check protocol capabilities, not an exact Codex version allowlist."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import sys

ADAPTER = 4


def executable(path):
    """Resolve the native executable behind the official npm launcher when present."""
    real = Path(path).resolve()
    if real.suffix == ".js":
        root = real.parent.parent
        candidates = list(
            (root / "node_modules" / "@openai").glob("codex-*/vendor/*/bin/codex")
        )
        candidates += list((root / "vendor").glob("*/bin/codex"))
        import platform

        architecture = "aarch64" if platform.machine() == "arm64" else "x86_64"
        candidates = [
            p for p in candidates if architecture in str(p) and "darwin" in str(p)
        ]
        if len(candidates) == 1:
            return candidates[0].resolve()
    return real


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(data)
    return value.hexdigest()


def validate_schema(root):
    request = json.loads((root / "ClientRequest.json").read_text())
    methods = {
        m
        for x in request["oneOf"]
        for m in x.get("properties", {}).get("method", {}).get("enum", [])
    }
    required = {
        "initialize",
        "account/read",
        "account/login/start",
        "account/rateLimits/read",
        "thread/start",
        "thread/resume",
        "turn/start",
        "turn/interrupt",
    }
    if required - methods:
        raise ValueError("Required account/thread methods changed.")
    login = json.loads((root / "v2/LoginAccountParams.json").read_text())
    variants = [
        x
        for x in login["oneOf"]
        if x.get("properties", {}).get("type", {}).get("enum") == ["chatgptAuthTokens"]
    ]
    if not variants or not {"accessToken", "chatgptAccountId"} <= set(
        variants[0]["properties"]
    ):
        raise ValueError("Live token login interface changed.")
    if set(variants[0].get("required", [])) - {
        "type",
        "accessToken",
        "chatgptAccountId",
        "chatgptPlanType",
    }:
        raise ValueError("Token login requires new fields.")
    for name, fields in {
        "v2/TurnStartParams.json": {"threadId", "input"},
        "v2/TurnCompletedNotification.json": {"threadId", "turn"},
        "v2/GetAccountRateLimitsResponse.json": {"rateLimits"},
        "ChatgptAuthTokensRefreshParams.json": {"reason", "previousAccountId"},
    }.items():
        value = json.loads((root / name).read_text())
        if not fields <= set(value["properties"]):
            raise ValueError("Required response fields changed.")
        if name == "v2/TurnStartParams.json" and set(value.get("required", [])) - {
            "threadId",
            "input",
        }:
            raise ValueError("Turn continuation requires new fields.")
    # Validate complete request examples against the generated combined schema,
    # including types and newly required fields, not just matching field names.
    try:
        import jsonschema
    except ImportError:
        raise ValueError(
            "Install requirements.txt; JSON schema validation is required."
        ) from None
    if request.get("definitions"):
        validator = jsonschema.Draft7Validator(request)
        ident = "00000000-0000-4000-8000-000000000000"
        examples = [
            {
                "id": "probe",
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "local_codex_accounts", "version": "0.3.0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
            {
                "id": "probe",
                "method": "account/login/start",
                "params": {
                    "type": "chatgptAuthTokens",
                    "accessToken": "probe",
                    "chatgptAccountId": "probe",
                },
            },
            {
                "id": "probe",
                "method": "account/read",
                "params": {"refreshToken": False},
            },
            {"id": "probe", "method": "account/rateLimits/read"},
            {
                "id": "probe",
                "method": "thread/resume",
                "params": {
                    "threadId": ident,
                    "sandbox": "danger-full-access",
                    "approvalPolicy": "never",
                },
            },
            {
                "id": "probe",
                "method": "turn/start",
                "params": {
                    "threadId": ident,
                    "input": [{"type": "text", "text": "probe", "text_elements": []}],
                    "clientUserMessageId": ident,
                },
            },
            {
                "id": "probe",
                "method": "thread/turns/list",
                "params": {
                    "threadId": ident,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "summary",
                },
            },
        ]
        for sample in examples:
            if not validator.is_valid(sample):
                raise ValueError(
                    "Native RPC request contract changed: " + sample["method"]
                )


def check(binary, state):
    """Cache by launcher bytes/version; serialize callers with caller's file lock."""
    version = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, check=True, timeout=15
    ).stdout.strip()
    native = executable(binary)
    fingerprint = hashlib.sha256(
        (digest(binary) + digest(native) + version + str(ADAPTER)).encode()
    ).hexdigest()
    cache = state / "compatibility.json"
    if cache.exists():
        try:
            value = json.loads(cache.read_text())
            if value.get("fingerprint") == fingerprint:
                return value
        except (ValueError, OSError):
            pass
    with tempfile.TemporaryDirectory(prefix="codex-account-schema-") as folder:
        root = Path(folder)
        subprocess.run(
            [
                binary,
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                folder,
            ],
            capture_output=True,
            check=True,
            timeout=45,
        )
        validate_schema(root)
    help_text = subprocess.run(
        [binary, "--help"], capture_output=True, text=True, check=True, timeout=15
    ).stdout
    if not all(flag in help_text for flag in ["--remote", "--remote-auth-token-env"]):
        raise ValueError("Terminal remote connection flags changed.")
    # New releases must also accept the live login operation twice in the same
    # native process. Fake identities only; an OS sandbox denies all networking.
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("probe_native.py")),
            "--binary",
            binary,
        ],
        capture_output=True,
        check=True,
        timeout=40,
    )
    value = {
        "version": version,
        "fingerprint": fingerprint,
        "adapter": ADAPTER,
        "native_executable": str(native),
        "native_sha256": digest(native),
        "validation": "schema, CLI flags, network-denied native login-switch probe; real handshake on launch",
    }
    temporary = cache.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(cache)
    return value
