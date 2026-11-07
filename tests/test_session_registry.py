"""Live workers must survive failed probes and recover lost registration rows."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from pool import Pool, process_birth
from settings import atomic_json
import pool as pool_module


class RegistryRecoveryTests(unittest.TestCase):
    def test_unknown_birth_probe_does_not_delete_session_or_thread_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Pool(Path(folder))
            p.assign("work")
            p.claim("conversation")
            with patch("pool.process_birth", return_value=""):
                observer = Pool(Path(folder))
                self.assertEqual(observer.detail()[0]["id"], p.session)
                self.assertEqual(observer.owner("conversation"), p.session)
                observer.close()
            p.close()

    def test_positive_pid_reuse_still_removes_session_and_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Pool(Path(folder))
            p.assign("work")
            p.claim("conversation")
            with patch("pool.process_birth", return_value="different birth"):
                observer = Pool(Path(folder))
                self.assertEqual(observer.detail(), [])
                self.assertIsNone(observer.owner("conversation"))
                observer.close()
            p.close()

    def test_worker_heartbeat_restores_accidentally_removed_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Pool(Path(folder))
            p.assign("work")
            with p.db() as db:
                db.execute("DELETE FROM sessions")
            p.touch(state="working")
            self.assertEqual(p.detail()[0]["account"], "work")
            p.close()

    def fixture(self, root):
        p = Pool(root)
        p.assign("work")
        p.touch(state="working", threads=["conversation"])
        p.claim("conversation")
        (root / "services").mkdir()
        atomic_json(
            root / "services" / (p.session + ".connection.json"),
            {
                "pid": os.getpid(),
                "birth": process_birth(os.getpid()),
                "thread": "conversation",
                "token": "PRIVATE_NOT_TO_LOG",
            },
        )
        return p

    def test_old_worker_registration_and_claim_recover_from_matching_live_metadata(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            p = self.fixture(root)
            with p.db() as db:
                db.execute("DELETE FROM sessions")
                db.execute("DELETE FROM threads")
            observer = Pool(root)
            recovered = observer.detail()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0]["id"], p.session)
            self.assertEqual(recovered[0]["account"], "work")
            self.assertEqual(observer.owner("conversation"), p.session)
            self.assertNotIn("PRIVATE_NOT_TO_LOG", json.dumps(recovered))
            observer.close()
            p.close()

    def test_missing_claim_recovers_even_if_heartbeat_has_already_restored_session(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            p = self.fixture(root)
            with p.db() as db:
                db.execute("DELETE FROM threads")
            self.assertEqual(Pool(root).owner("conversation"), p.session)
            p.close()

    def test_recovery_never_steals_an_existing_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            p = self.fixture(root)
            other = Pool(root)
            other.assign("other")
            with p.db() as db:
                db.execute(
                    "UPDATE threads SET owner=? WHERE thread=?",
                    (other.session, "conversation"),
                )
                db.execute("DELETE FROM sessions WHERE id=?", (p.session,))
            self.assertEqual(other.owner("conversation"), other.session)
            other.close()
            p.close()

    def test_stale_mismatched_or_unverifiable_metadata_does_not_resurrect_workers(self):
        for fault in ("stale", "mismatch", "unknown", "dead"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                p = self.fixture(root)
                with p.db() as db:
                    db.execute("DELETE FROM sessions")
                    db.execute("DELETE FROM threads")
                    if fault == "stale":
                        db.execute(
                            "UPDATE session_details SET seen=?", (time.time() - 120,)
                        )
                    if fault == "mismatch":
                        db.execute("UPDATE session_details SET birth='other'")
                birth = process_birth(os.getpid())
                with (
                    patch(
                        "pool.process_birth",
                        return_value="" if fault == "unknown" else birth,
                    ),
                    patch.object(Pool, "alive", return_value=fault != "dead"),
                ):
                    observer = Pool(root)
                    self.assertEqual(observer.detail(), [])
                    observer.close()
                p.close()

    def test_unsuccessful_own_birth_probe_is_retried_instead_of_cached_forever(self):
        with (
            patch.object(pool_module, "_own_birth_cache", (None, "")),
            patch("pool._birth", side_effect=["", "valid-start"]) as probe,
        ):
            self.assertEqual(pool_module._own_birth(), "")
            self.assertEqual(pool_module._own_birth(), "valid-start")
            self.assertEqual(pool_module._own_birth(), "valid-start")
            self.assertEqual(probe.call_count, 2)

    def test_status_reader_does_not_register_itself_as_a_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Pool(Path(folder))
            p.touch(state="inspecting")
            self.assertEqual(p.detail(), [])
            p.close()
