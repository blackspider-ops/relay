#!/usr/bin/env python3
"""Offline test double for native JSONL transport. Never contacts OpenAI."""

import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
import fcntl

home = Path(os.environ.get("CODEX_HOME", "."))
home.mkdir(parents=True, exist_ok=True)
state_file = home / "fake-history.json"
guard = threading.RLock()
out = threading.Lock()


def send(value):
    with out:
        print(json.dumps(value), flush=True)


def change(operation):
    with guard:
        with (home / "fake-history.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = (
                json.loads(state_file.read_text())
                if state_file.exists()
                else {"threads": {}, "calls": []}
            )
            result = operation(data)
            temporary = state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(data))
            temporary.replace(state_file)
            return result


def finish(thread, turn, text, mode):
    status = "failed" if mode in {"quota", "capacity"} else "completed"
    error = (
        {"message": "synthetic limit", "codexErrorInfo": "usageLimitExceeded"}
        if mode == "quota"
        else None
    )
    if mode == "capacity":
        error = {
            "message": "Selected model is at capacity. Please try a different model.",
            "codexErrorInfo": "policyCapacityUnavailable",
        }

    def apply(data):
        value = data["threads"][thread]
        value["status"] = {"type": "idle"}
        value["turns"][-1].update(status=status, error=error)
        return value["turns"][-1].copy()

    value = change(apply)
    if status == "completed":
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread,
                    "item": {
                        "id": "reply-" + turn,
                        "type": "agentMessage",
                        "text": "FAKE_OK",
                    },
                },
            }
        )
    if mode == "capacity":
        send(
            {
                "method": "error",
                "params": {
                    "threadId": thread,
                    "turnId": turn,
                    "error": error,
                    "willRetry": False,
                },
            }
        )
    send({"method": "turn/completed", "params": {"threadId": thread, "turn": value}})
    send(
        {
            "method": "thread/status/changed",
            "params": {"threadId": thread, "status": {"type": "idle"}},
        }
    )


if "--version" in sys.argv:
    print("codex-cli 0.166.0")
    raise SystemExit()
if "--help" in sys.argv:
    print("--remote --remote-auth-token-env")
    raise SystemExit()

for raw in sys.stdin:
    try:
        request = json.loads(raw)
        method = request.get("method")
        params = request.get("params") or {}
        ident = request.get("id")
        if ident is None:
            continue
        change(
            lambda d: d["calls"].append(
                {"method": method, "thread": params.get("threadId")}
            )
        )
        result = {}
        if method == "initialize":
            result = {
                "userAgent": "fake/1",
                "codexHome": str(home),
                "platformFamily": "unix",
                "platformOs": "macos",
            }
        elif method == "account/read":
            result = {"account": {"type": "chatgpt", "planType": "plus"}}
        elif method == "account/login/start":
            result = {"type": params["type"]}
        elif method == "account/rateLimits/read":
            reset_count = change(lambda d: d.get("reset_credit_count", 1))
            result = {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "primary": {"usedPercent": 20, "resetsAt": int(time.time()) + 3600}
                },
                "rateLimitResetCredits": {
                    "availableCount": reset_count,
                    "credits": None,
                },
            }
        elif method == "account/rateLimitResetCredit/consume":

            def redeem(data):
                keys = data.setdefault("reset_keys", [])
                key = params["idempotencyKey"]
                if key in keys:
                    return {"outcome": "alreadyRedeemed"}
                count = data.get("reset_credit_count", 1)
                if count <= 0:
                    return {"outcome": "noCredit"}
                keys.append(key)
                data["reset_credit_count"] = count - 1
                return {"outcome": "reset"}

            result = change(redeem)
        elif method in ("thread/start", "thread/fork"):
            thread = str(uuid.uuid4())
            value = {
                "id": thread,
                "cwd": params.get("cwd", str(home)),
                "model": params.get("model", "fake-model"),
                "canAcceptDirectInput": True,
                "status": {"type": "idle"},
                "turns": [],
            }
            change(lambda d: d["threads"].update({thread: value}))
            result = {
                "thread": value,
                "model": value["model"],
                "cwd": value["cwd"],
                "approvalPolicy": params.get("approvalPolicy", "never"),
                "sandbox": params.get("sandbox", "read-only"),
            }
            send({"method": "thread/started", "params": {"thread": value}})
        elif method == "thread/resume":
            value = change(lambda d: d["threads"].get(params["threadId"]))
            if not value:
                raise ValueError("no thread")
            if (
                value.get("runtime") != os.getpid()
                and value.get("status", {}).get("type") == "active"
            ):
                value["status"] = {"type": "idle"}
                if value["turns"]:
                    value["turns"][-1].update(status="interrupted", error=None)
            value.update({k: v for k, v in params.items() if k in {"model", "cwd"}})
            change(lambda d: d["threads"].update({value["id"]: value}))
            result = {"thread": value, "model": value["model"], "cwd": value["cwd"]}
        elif method == "thread/read":
            result = {"thread": change(lambda d: d["threads"][params["threadId"]])}
        elif method == "thread/turns/list":
            result = {
                "data": change(
                    lambda d: list(reversed(d["threads"][params["threadId"]]["turns"]))[
                        : params.get("limit", 1)
                    ]
                )
            }
        elif method == "thread/loaded/list":
            result = {"data": change(lambda d: list(d["threads"]))}
        elif method in {"turn/start", "review/start"}:
            thread = params["threadId"]
            turn = str(uuid.uuid4())
            value = {"id": turn, "status": "inProgress", "items": [], "error": None}

            def begin(data):
                data["threads"][thread]["turns"].append(value)
                data["threads"][thread]["status"] = {
                    "type": "active",
                    "activeFlags": [],
                }
                data["threads"][thread]["runtime"] = os.getpid()
                return sum(
                    c["method"] in {"turn/start", "review/start"} for c in data["calls"]
                )

            count = change(begin)
            fault = (
                json.loads((home / "fault.json").read_text())
                if (home / "fault.json").exists()
                else {}
            )
            mode = "quota" if count <= fault.get("quota_turns", 0) else "ok"
            if count <= fault.get("capacity_turns", 0):
                mode = "capacity"
            if not fault.get("drop_ack"):
                send({"id": ident, "result": {"turn": value}})
            send(
                {
                    "method": "turn/started",
                    "params": {"threadId": thread, "turn": value},
                }
            )
            if fault.get("external"):
                send(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": thread,
                            "item": {"id": "external1", "type": "commandExecution"},
                        },
                    }
                )
            if fault.get("die_once") and count == 1:
                os._exit(11)
            timer = threading.Timer(
                float(fault.get("delay", 0.02)), finish, args=(thread, turn, "", mode)
            )
            timer.daemon = True
            timer.start()
            continue
        elif method == "turn/interrupt":
            result = {}
        send({"id": ident, "result": result})
    except Exception:
        send(
            {
                "id": request.get("id"),
                "error": {"code": -32600, "message": "Synthetic request rejected"},
            }
        )
