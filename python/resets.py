"""Explicit earned-reset redemption. Never called by automatic recovery."""

import hashlib
import json
import time
import uuid

from pool import Pool, blocked, reset_time
from settings import atomic_json

OUTCOMES = {"reset", "alreadyRedeemed", "nothingToReset", "noCredit"}


def available(snapshot):
    summary = (snapshot or {}).get("rateLimitResetCredits")
    count = summary.get("availableCount") if isinstance(summary, dict) else None
    return count if type(count) is int and count >= 0 else None


def identity(home):
    marker = (home / "enrolled").read_bytes()
    return json.loads(marker).get("identity") or hashlib.sha256(marker).hexdigest()


def receipt(home):
    path = home / "reset-attempt.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(
            "Unreadable reset receipt. Check this account before trying again."
        )
    return value


def pending(home):
    try:
        value = receipt(home)
        return value.get("state") == "pending" and value.get("identity") == identity(
            home
        )
    except (OSError, ValueError):
        return False


def update_allowance(pool, label, snapshot):
    """Only verified allowance can lift a quota block. Keep other retry reasons."""
    if not isinstance(snapshot, dict) or not (
        isinstance(snapshot.get("rateLimits"), dict)
        or isinstance(snapshot.get("rateLimitsByLimitId"), dict)
        or type(snapshot.get("ordinaryUsageAllowed")) is bool
    ):
        raise ValueError(
            "Codex returned unrecognized allowance data; quota blocks were retained."
        )
    pool.save(label, snapshot)
    with pool.db() as db:
        scopes = [
            r[0]
            for r in db.execute(
                "SELECT scope FROM blocks WHERE label=? AND reason='quota'", (label,)
            )
        ]
        for scope in scopes:
            model = None if scope == "*" else scope
            if blocked(snapshot, model):
                until = reset_time(snapshot, model) or time.time() + 300
                db.execute(
                    "UPDATE blocks SET until=? WHERE label=? AND scope=? AND reason='quota'",
                    (until, label, scope),
                )
            else:
                db.execute(
                    "DELETE FROM blocks WHERE label=? AND scope=? AND reason='quota'",
                    (label, scope),
                )
    pool.signal()


async def consume(state, label, binary, request_id=None):
    from router import Account, RouterError, RemoteError, account_home, labels

    if label not in labels(state):
        raise RouterError("Choose an enrolled account from relay list.")
    if request_id is not None:
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError):
            raise RouterError("Use a UUID for --request-id.") from None
    home = account_home(state, label)
    pool = Pool(state)
    account = Account(label, home, binary, pool)
    try:
        # The same per-account credential lock is used by old and new brokers.
        # A reset does not require replacing the coordinator or a task's login.
        async with account.operation() as rpc:
            signed_in = identity(home)
            previous = receipt(home)
            same_identity = previous.get("identity") == signed_in
            retry = same_identity and previous.get("state") == "pending"
            known = (
                same_identity
                and request_id is not None
                and previous.get("request_id") == request_id
                and previous.get("state") == "done"
            )
            key = (
                previous.get("request_id") if retry else request_id or str(uuid.uuid4())
            )
            if retry:
                try:
                    key = str(uuid.UUID(key))
                except (ValueError, AttributeError, TypeError):
                    raise RouterError(
                        "The pending reset receipt is unreadable. Check account usage before trying again."
                    ) from None
            outcome = previous.get("outcome") if known else None
            if known and (not isinstance(outcome, str) or outcome not in OUTCOMES):
                raise RouterError(
                    "The saved reset result is unreadable. Check account usage before trying again."
                )
            if known and outcome == "reset":
                outcome = "alreadyRedeemed"
            if not retry and not known:
                current = await rpc.call("account/rateLimits/read")
                update_allowance(pool, label, current)
                count = available(current)
                if count == 0:
                    return {"outcome": "noCredit", "refreshed": True}
                if count is None:
                    raise RouterError(
                        "Codex did not report reset availability. Check allowance again before using a reset."
                    )
            if not known:
                attempt = {
                    "request_id": key,
                    "identity": signed_in,
                    "state": "pending",
                    "at": time.time(),
                }
                atomic_json(home / "reset-attempt.json", attempt)
                pool.invalidate(label)
                pool.signal()
                try:
                    response = await rpc.call(
                        "account/rateLimitResetCredit/consume", {"idempotencyKey": key}
                    )
                    outcome = response.get("outcome")
                except RemoteError as error:
                    if error.payload.get("code") in {-32601, -32602}:
                        atomic_json(
                            home / "reset-attempt.json",
                            {**attempt, "state": "rejected"},
                        )
                        raise RouterError(
                            "This Codex runtime cannot use resets through Relay. Update Codex and try again."
                        ) from None
                    raise RouterError(
                        "Reset result could not be confirmed. Try this action again; Relay will reuse the same pending request."
                    ) from None
                except Exception:
                    raise RouterError(
                        "Reset result could not be confirmed. Try this action again; Relay will reuse the same pending request."
                    ) from None
                if not isinstance(outcome, str) or outcome not in OUTCOMES:
                    raise RouterError(
                        "Reset result was not recognized. Try again to check the same pending request."
                    )
                atomic_json(
                    home / "reset-attempt.json",
                    {**attempt, "state": "done", "outcome": outcome},
                )
            refreshed = False
            try:
                snapshot = await rpc.call("account/rateLimits/read")
                update_allowance(pool, label, snapshot)
                refreshed = True
            except Exception:
                pool.invalidate(label)
                pool.signal()
            pool.record(
                "reset_checked", account=label, outcome=outcome, refreshed=refreshed
            )
            return {"outcome": outcome, "refreshed": refreshed}
    finally:
        pool.close()


def message(result):
    text = {
        "reset": "Used one usage reset.",
        "alreadyRedeemed": "The previous reset was already used; no additional reset was spent.",
        "nothingToReset": "No usage window is eligible for a reset; no reset was used.",
        "noCredit": "No usage resets are available; no reset was used.",
    }[result["outcome"]]
    return text + (
        " Allowance updated."
        if result["refreshed"]
        else " Allowance could not be refreshed; choose Check allowance now."
    )
