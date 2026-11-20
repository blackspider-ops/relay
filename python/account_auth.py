"""Account login health metadata. Never stores credentials or server payloads."""

import json
import re
import time

from settings import atomic_json

PERMANENT_REFRESH_MESSAGES = {
    "Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again.",
    "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.",
    "Your access token could not be refreshed because your refresh token was revoked. Please log out and sign in again.",
}


def required(home):
    try:
        saved = json.loads((home / "login-status.json").read_text())
        return (
            saved.get("state") == "sign_in_required"
            and saved.get("enrollment") == (home / "enrolled").stat().st_mtime_ns
        )
    except (OSError, ValueError, TypeError):
        return False


def mark_required(home, *, enrollment=None):
    atomic_json(
        home / "login-status.json",
        {
            "state": "sign_in_required",
            "enrollment": enrollment
            if enrollment is not None
            else (home / "enrolled").stat().st_mtime_ns,
            "checked_at": time.time(),
        },
    )


def clear(home):
    (home / "login-status.json").unlink(missing_ok=True)


def permanent(payload):
    return (
        payload.get("code") == -32603
        and isinstance(payload.get("message"), str)
        and payload.get("message") in PERMANENT_REFRESH_MESSAGES
    )


def usage_unauthorized(payload):
    """Recognize the native usage endpoint's auth envelope, not arbitrary 401 text."""
    message = payload.get("message")
    return (
        payload.get("code") == -32603
        and isinstance(message, str)
        and message.startswith("failed to fetch codex rate limits:")
        and re.search(r"\bfailed: 401 Unauthorized(?:;|$)", message) is not None
    )
