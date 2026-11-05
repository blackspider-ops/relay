"""Verify installed native login controls using fake identities, with network denied.

This does not verify server authorization, model generation, or quota failover.
"""

import asyncio
import base64
import json
import os
import tempfile
import time
import argparse

from router import RPC, codex_path


def synthetic_token(account):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return ".".join(
        [
            encode({"alg": "none", "typ": "JWT"}),
            encode(
                {
                    "exp": int(time.time()) + 3600,
                    "email": f"{account}@example.invalid",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": account,
                        "chatgpt_user_id": account,
                        "chatgpt_plan_type": "plus",
                    },
                }
            ),
            "fake-signature",
        ]
    )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=None)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="codex-accounts-offline-probe-") as folder:
        env = dict(os.environ)
        env["CODEX_HOME"] = folder
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            env.pop(key, None)
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1) (allow default) (deny network*)",
            args.binary or codex_path(),
            "app-server",
            "--stdio",
            "-c",
            'cli_auth_credentials_store="ephemeral"',
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rpc = RPC(proc)
        pid = proc.pid
        try:
            await rpc.initialize()
            for account in ("synthetic-one", "synthetic-two"):
                result = await rpc.call(
                    "account/login/start",
                    {
                        "type": "chatgptAuthTokens",
                        "accessToken": synthetic_token(account),
                        "chatgptAccountId": account,
                        "chatgptPlanType": "plus",
                    },
                )
                assert result.get("type") == "chatgptAuthTokens"
                assert proc.pid == pid and proc.returncode is None
            print(
                "PASS: native app-server accepted two synthetic login changes in one process."
            )
            print("Network denied by OS sandbox. No real accounts or model calls used.")
            print(
                "Not verified: real authentication, thread continuation, or actual quota handoff."
            )
        finally:
            await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())
