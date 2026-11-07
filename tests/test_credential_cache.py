"""One native credential read can serve concurrent sessions without extra prompts."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import account_auth
import broker
from router import KeychainAccessRequired, RemoteError
from test_services import wait_for


def token(account="identity", expiry=None):
    claims = {
        "exp": expiry if expiry is not None else time.time() + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account,
            "chatgpt_plan_type": "plus",
        },
    }
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "fake." + part + ".fake"


class CredentialCacheTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, state, label="one"):
        home = state / "accounts" / label
        home.mkdir(parents=True)
        (home / "enrolled").write_text(
            json.dumps({"identity": hashlib.sha256(b"identity").hexdigest()})
        )
        return home

    def test_memory_cache_expires_and_rejects_other_enrollment_generation(self):
        now = [1000]
        c = broker.CredentialCache(clock=lambda: now[0])
        value = {"accessToken": token(expiry=4600), "chatgptAccountId": "identity"}
        c.put("one", 1, value)
        self.assertEqual(c.get("one", 1), value)
        now[0] = 4490
        self.assertIsNone(c.get("one", 1))
        now[0] = 1000
        c.put("one", 1, value)
        self.assertIsNone(c.get("one", 2))
        c.put("one", 2, value)
        c.discard("one")
        self.assertIsNone(c.get("one", 2))

    async def test_six_clients_share_native_export_refresh_and_invalidation(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            home = self.fixture(state)
            calls = []
            access = token()

            class Account:
                def __init__(self, label, home, binary, pool):
                    self.label = label
                    self.home = home
                    self.rpc = self

                async def start(self):
                    pass

                async def close(self):
                    pass

                async def call(self, method, params=None):
                    calls.append(method)
                    if method == "getAuthStatus":
                        return {"authMethod": "chatgpt", "authToken": access}
                    return {"account": {"type": "chatgpt"}}

            with (
                patch("router.Account", Account),
                patch(
                    "router.read_keychain",
                    side_effect=AssertionError("Native export must avoid security"),
                ) as reader,
            ):
                server = asyncio.create_task(broker.serve(state, "unused"))
                try:
                    await wait_for(lambda: broker.paths(state)[1].exists())
                    values = await asyncio.gather(
                        *(
                            broker.exchange(
                                state, {"op": "credentials", "label": "one"}
                            )
                            for _ in range(6)
                        )
                    )
                    self.assertTrue(all(x["accessToken"] == access for x in values))
                    self.assertEqual(calls, ["getAuthStatus"])
                    await asyncio.gather(
                        *(
                            broker.exchange(
                                state,
                                {"op": "credentials", "label": "one", "refresh": True},
                            )
                            for _ in range(6)
                        )
                    )
                    self.assertEqual(calls.count("account/read"), 1)
                    self.assertEqual(calls.count("getAuthStatus"), 2)
                    await broker.exchange(state, {"op": "invalidate", "label": "one"})
                    await broker.exchange(state, {"op": "credentials", "label": "one"})
                    self.assertEqual(calls.count("getAuthStatus"), 3)
                    (home / "enrolled").write_text(
                        json.dumps(
                            {
                                "identity": hashlib.sha256(b"identity").hexdigest(),
                                "new_generation": True,
                            }
                        )
                    )
                    await broker.exchange(state, {"op": "credentials", "label": "one"})
                    self.assertEqual(calls.count("getAuthStatus"), 4)
                    reader.assert_not_called()
                finally:
                    await broker.exchange(state, {"op": "stop"})
                    await asyncio.wait_for(server, 3)

    async def test_keychain_denial_does_not_spawn_more_reads_or_invalidate_login(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            homes = [self.fixture(state, label) for label in ("one", "two")]

            class Account:
                def __init__(self, label, home, binary, pool):
                    self.label = label
                    self.home = home
                    self.rpc = self

                async def start(self):
                    pass

                async def close(self):
                    pass

                async def call(self, method, params=None):
                    raise RemoteError({"code": -32601, "message": "unsupported"})

            with (
                patch("router.Account", Account),
                patch(
                    "router.read_keychain", side_effect=KeychainAccessRequired()
                ) as reader,
            ):
                server = asyncio.create_task(broker.serve(state, "unused"))
                try:
                    await wait_for(lambda: broker.paths(state)[1].exists())
                    for label in ["one"] * 6 + ["two"] * 6:
                        with self.assertRaises(broker.CoordinatorError) as raised:
                            await broker.exchange(
                                state, {"op": "credentials", "label": label}
                            )
                        self.assertEqual(
                            raised.exception.reason, "keychain_access_required"
                        )
                    self.assertEqual(reader.call_count, 1)
                    self.assertTrue(
                        all(not account_auth.required(home) for home in homes)
                    )
                finally:
                    await broker.exchange(state, {"op": "stop"})
                    await asyncio.wait_for(server, 3)

    async def test_two_account_helpers_keep_separate_homes_and_cached_identities(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            for label in ("one", "two"):
                home = self.fixture(state, label)
                (home / "enrolled").write_text(
                    json.dumps({"identity": hashlib.sha256(label.encode()).hexdigest()})
                )
            homes = {}
            calls = []

            class Account:
                def __init__(self, label, home, binary, pool):
                    self.label, self.home, self.rpc = label, home, self
                    homes[label] = home

                async def start(self):
                    pass

                async def close(self):
                    pass

                async def call(self, method, params=None):
                    calls.append((self.label, method))
                    return {"authMethod": "chatgpt", "authToken": token(self.label)}

            with (
                patch("router.Account", Account),
                patch(
                    "router.read_keychain",
                    side_effect=AssertionError("No security subprocess"),
                ),
            ):
                server = asyncio.create_task(broker.serve(state, "unused"))
                try:
                    await wait_for(lambda: broker.paths(state)[1].exists())
                    labels = ["one", "two"] * 6
                    results = await asyncio.gather(
                        *(
                            broker.exchange(
                                state, {"op": "credentials", "label": label}
                            )
                            for label in labels
                        )
                    )
                    self.assertEqual([v["chatgptAccountId"] for v in results], labels)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(
                        homes,
                        {label: state / "accounts" / label for label in ("one", "two")},
                    )
                finally:
                    await broker.exchange(state, {"op": "stop"})
                    await asyncio.wait_for(server, 3)

    async def test_removed_native_endpoint_uses_keychain_compatibility_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            home = self.fixture(Path(folder))
            expected = {"accessToken": token(), "chatgptAccountId": "identity"}

            class Account:
                async def start(self):
                    pass

                async def call(self, *args):
                    raise RemoteError({"code": -32601})

            a = Account()
            a.home = home
            a.rpc = a
            with patch("router.read_keychain", return_value=expected) as reader:
                self.assertEqual(await broker.native_credentials(a), expected)
                reader.assert_called_once_with(home)

    async def test_exported_account_mismatch_is_never_returned_as_enrolled_identity(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            home = self.fixture(Path(folder))
            expected = {"accessToken": token(), "chatgptAccountId": "identity"}

            class Account:
                async def start(self):
                    pass

                async def call(self, *args):
                    return {
                        "authMethod": "chatgpt",
                        "authToken": token("different-identity"),
                    }

            a = Account()
            a.home = home
            a.rpc = a
            with patch("router.read_keychain", return_value=expected):
                self.assertEqual(await broker.native_credentials(a), expected)
