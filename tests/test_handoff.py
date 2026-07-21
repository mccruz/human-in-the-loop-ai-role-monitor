from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from role_monitor.handoff import (
    InMemoryTaskAdapter,
    WebhookTaskAdapter,
    build_manifest,
    complete_receipt,
    deliver_manifest,
    redact_external_error,
    verify_receipt,
    write_manifest_atomic,
)
from role_monitor.models import JobPosting, ScoredPosting
from role_monitor.review import ReviewService
from role_monitor.store import RoleStore


def add_approved(store: RoleStore, identifier: str = "one") -> str:
    record = store.upsert_scored(ScoredPosting(JobPosting("lever", identifier, "Example", "Automation Engineer", f"https://jobs.example.test/{identifier}"), 88, "strong", ("AI",)))
    ReviewService(store).approve(record["role_key"])
    return record["role_key"]


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RoleStore()

    def tearDown(self) -> None:
        self.store.close()

    def test_manifest_is_deterministic_and_delivery_is_idempotent(self) -> None:
        key = add_approved(self.store)
        manifest = build_manifest(self.store)
        self.assertEqual(manifest, build_manifest(self.store))
        adapter = InMemoryTaskAdapter()
        deliver_manifest(self.store, manifest, adapter)
        self.assertIsNotNone(self.store.get(key)["delivered_at"])
        adapter.tasks[key]["state"] = "in_progress"
        deliver_manifest(self.store, manifest, adapter)
        self.assertEqual(adapter.tasks[key]["state"], "in_progress")

    def test_completed_replay_skips_the_external_adapter(self) -> None:
        add_approved(self.store)
        manifest = build_manifest(self.store)
        adapter = InMemoryTaskAdapter()
        deliver_manifest(self.store, manifest, adapter)

        class FailingIfCalled:
            def upsert_manifest(self, _manifest):
                raise AssertionError("completed manifests must not be redelivered")

        self.assertEqual(complete_receipt(manifest), deliver_manifest(self.store, manifest, FailingIfCalled()))

    def test_partial_wrong_and_stale_receipts_change_nothing(self) -> None:
        key = add_approved(self.store)
        manifest = build_manifest(self.store)
        good = complete_receipt(manifest)
        for receipt in (
            {**good, "roles": []},
            {**good, "manifest_id": "manifest-stale"},
            {**good, "roles": [{"role_key": key, "url": "https://jobs.example.test/wrong"}]},
        ):
            with self.assertRaises(ValueError):
                verify_receipt(manifest, receipt)
            self.assertIsNone(self.store.get(key)["delivered_at"])

    def test_tampered_manifest_digest_is_rejected(self) -> None:
        add_approved(self.store)
        manifest = build_manifest(self.store)
        tampered = {**manifest, "roles": [{**manifest["roles"][0], "score": 999}]}
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_receipt(tampered, complete_receipt(manifest))

    def test_unsigned_extra_manifest_field_is_rejected(self) -> None:
        add_approved(self.store)
        manifest = build_manifest(self.store)
        with self.assertRaisesRegex(ValueError, "top-level"):
            verify_receipt({**manifest, "destination": "unreviewed"}, complete_receipt(manifest))

    def test_registration_rejects_role_content_outside_approval_snapshot(self) -> None:
        add_approved(self.store)
        manifest = build_manifest(self.store)
        tampered = {
            **manifest,
            "manifest_id": "manifest-unreviewed-content",
            "roles": [{**manifest["roles"][0], "url": "https://jobs.example.test/unreviewed"}],
        }
        with self.assertRaisesRegex(ValueError, "immutable approval snapshot"):
            self.store.register_manifest(tampered)

    def test_stale_manifest_receipt_cannot_mutate_roles(self) -> None:
        first_key = add_approved(self.store, "first")
        manifest_a = build_manifest(self.store)
        second_key = add_approved(self.store, "second")
        manifest_b = build_manifest(self.store)
        stale_adapter = InMemoryTaskAdapter()
        with self.assertRaisesRegex(ValueError, "stale"):
            deliver_manifest(self.store, manifest_a, stale_adapter)
        self.assertEqual({}, stale_adapter.tasks)
        self.assertIsNone(self.store.get(first_key)["delivered_at"])
        self.assertIsNone(self.store.get(second_key)["delivered_at"])
        deliver_manifest(self.store, manifest_b, InMemoryTaskAdapter())
        self.assertIsNotNone(self.store.get(first_key)["delivered_at"])
        self.assertIsNotNone(self.store.get(second_key)["delivered_at"])

    def test_rescan_url_change_does_not_break_registered_manifest(self) -> None:
        key = add_approved(self.store)
        manifest = build_manifest(self.store)
        changed = ScoredPosting(
            JobPosting("lever", "one", "Example", "Automation Engineer", "https://jobs.example.test/one-new"),
            91,
            "strong",
            ("AI",),
        )
        self.store.upsert_scored(changed)
        deliver_manifest(self.store, manifest, InMemoryTaskAdapter())
        row = self.store.get(key)
        self.assertEqual("https://jobs.example.test/one-new", row["url"])
        self.assertIsNotNone(row["delivered_at"])

    def test_manifest_uses_the_human_approved_snapshot_after_listing_drift(self) -> None:
        key = add_approved(self.store)
        changed = ScoredPosting(
            JobPosting(
                "lever",
                "one",
                "Example",
                "Unreviewed Replacement Title",
                "https://jobs.example.test/unreviewed-replacement",
            ),
            99,
            "strong",
            ("changed after approval",),
        )
        self.store.upsert_scored(changed)
        manifest = build_manifest(self.store)
        self.assertEqual(key, manifest["roles"][0]["role_key"])
        self.assertEqual("Automation Engineer", manifest["roles"][0]["title"])
        self.assertEqual("https://jobs.example.test/one", manifest["roles"][0]["url"])

    def test_concurrent_delivery_claim_allows_one_adapter_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "roles.sqlite3"
            first_store = RoleStore(database)
            add_approved(first_store)
            manifest = build_manifest(first_store)
            first_store.close()

            entered = threading.Event()
            release = threading.Event()
            calls: list[str] = []
            errors: list[BaseException] = []

            class BlockingAdapter:
                def upsert_manifest(self, payload):
                    calls.append(payload["manifest_id"])
                    entered.set()
                    if not release.wait(timeout=2):
                        raise TimeoutError("test adapter was not released")
                    return complete_receipt(payload)

            def deliver_first() -> None:
                store = RoleStore(database)
                try:
                    deliver_manifest(store, manifest, BlockingAdapter())
                except BaseException as error:  # pragma: no cover - asserted after join
                    errors.append(error)
                finally:
                    store.close()

            worker = threading.Thread(target=deliver_first)
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            second_store = RoleStore(database)
            try:
                with self.assertRaisesRegex(ValueError, "already in progress"):
                    deliver_manifest(second_store, manifest, InMemoryTaskAdapter())
            finally:
                second_store.close()
                release.set()
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([manifest["manifest_id"]], calls)
            final_store = RoleStore(database)
            try:
                self.assertEqual("completed", final_store.manifest_status(manifest))
            finally:
                final_store.close()

    def test_stranded_delivery_requires_reconciliation_before_safe_replay(self) -> None:
        key = add_approved(self.store)
        manifest = build_manifest(self.store)
        adapter = InMemoryTaskAdapter()
        self.assertEqual("delivering", self.store.claim_manifest(manifest))
        lost_receipt = adapter.upsert_manifest(manifest)
        self.assertEqual("complete", lost_receipt["status"])
        adapter.tasks[key]["state"] = "in_progress"
        with self.assertRaisesRegex(ValueError, "already in progress"):
            deliver_manifest(self.store, manifest, adapter)

        self.store.recover_manifest(manifest)
        deliver_manifest(self.store, manifest, adapter)

        self.assertIsNotNone(self.store.get(key)["delivered_at"])
        self.assertEqual("in_progress", adapter.tasks[key]["state"])
        events = [event["event_type"] for event in self.store.audit_events(key)]
        self.assertIn("handoff_recovery_confirmed", events)

    def test_failed_delivery_is_invalidated_when_role_retired_in_flight(self) -> None:
        record = self.store.upsert_scored(
            ScoredPosting(
                JobPosting(
                    "lever", "in-flight", "Example", "Automation Engineer",
                    "https://jobs.example.test/in-flight", employer_key="example-board",
                ),
                88,
                "strong",
                ("AI",),
            ),
            employer_key="example-board",
        )
        ReviewService(self.store).approve(record["role_key"])
        manifest = build_manifest(self.store)
        self.store.claim_manifest(manifest)
        self.store.retire_missing("example-board", set())
        self.store.retire_missing("example-board", set())
        self.store.release_manifest(manifest)
        self.assertEqual("invalidated", self.store.manifest_status(manifest))
        self.assertEqual("pending", self.store.get(record["role_key"])["review_state"])
        with self.assertRaisesRegex(ValueError, "stale"):
            deliver_manifest(self.store, manifest, InMemoryTaskAdapter())

    def test_identical_valid_manifest_can_be_reactivated_after_set_churn(self) -> None:
        def add(identifier: str) -> str:
            record = self.store.upsert_scored(
                ScoredPosting(
                    JobPosting(
                        "lever", identifier, "Example", f"Automation Engineer {identifier}",
                        f"https://jobs.example.test/{identifier}", employer_key="example-board",
                    ),
                    88,
                    "strong",
                    ("AI",),
                ),
                employer_key="example-board",
            )
            ReviewService(self.store).approve(record["role_key"])
            return record["role_key"]

        first_key = add("first")
        manifest_a = build_manifest(self.store)
        add("second")
        manifest_ab = build_manifest(self.store)
        self.assertEqual("invalidated", self.store.manifest_status(manifest_a))
        self.store.retire_missing("example-board", {first_key})
        self.assertEqual("invalidated", self.store.manifest_status(manifest_ab))
        reactivated = build_manifest(self.store)
        self.assertEqual(manifest_a["manifest_id"], reactivated["manifest_id"])
        self.assertEqual("pending", self.store.manifest_status(reactivated))

    def test_delivered_role_retains_final_history_when_source_retires_and_reappears(self) -> None:
        posting = JobPosting(
            "lever", "delivered", "Example", "Automation Engineer",
            "https://jobs.example.test/delivered", employer_key="example-board",
        )
        record = self.store.upsert_scored(
            ScoredPosting(posting, 88, "strong", ("AI",)),
            employer_key="example-board",
        )
        ReviewService(self.store).approve(record["role_key"], "final approval")
        manifest = build_manifest(self.store)
        deliver_manifest(self.store, manifest, InMemoryTaskAdapter())
        self.store.retire_missing("example-board", set())
        self.store.retire_missing("example-board", set())
        retired = self.store.get(record["role_key"])
        self.assertFalse(retired["active"])
        self.assertEqual("approved", retired["review_state"])
        self.store.upsert_scored(
            ScoredPosting(posting, 90, "strong", ("AI",)),
            employer_key="example-board",
        )
        reappeared = self.store.get(record["role_key"])
        self.assertTrue(reappeared["active"])
        self.assertEqual("approved", reappeared["review_state"])
        self.assertIsNotNone(reappeared["delivered_at"])
        self.assertEqual([], self.store.pending_review())

    def test_stale_role_cannot_be_registered_or_claimed_for_handoff(self) -> None:
        key = add_approved(self.store)
        manifest = build_manifest(self.store)
        self.store._connection.execute(
            "UPDATE roles SET last_seen_at='2000-01-01T00:00:00+00:00' WHERE role_key=?",
            (key,),
        )
        self.store._connection.commit()

        with self.assertRaisesRegex(ValueError, "unavailable|stale"):
            self.store.register_manifest(manifest)
        self.assertEqual("pending", self.store.manifest_status(manifest))
        with self.assertRaisesRegex(ValueError, "unavailable|stale"):
            self.store.claim_manifest(manifest)
        self.assertEqual("invalidated", self.store.manifest_status(manifest))
        with self.assertRaisesRegex(ValueError, "No approved"):
            build_manifest(self.store)

    def test_pre_claim_database_schema_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE handoff_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'invalidated')),
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX handoff_manifests_status
                    ON handoff_manifests(status, created_at DESC);
                """
            )
            connection.close()
            migrated = RoleStore(database)
            try:
                columns = {
                    row["name"]
                    for row in migrated._connection.execute(
                        "PRAGMA table_info(handoff_manifests)"
                    ).fetchall()
                }
                schema = migrated._connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='handoff_manifests'"
                ).fetchone()["sql"]
                self.assertIn("claimed_at", columns)
                self.assertIn("delivering", schema)
                add_approved(migrated)
                manifest = build_manifest(migrated)
                deliver_manifest(migrated, manifest, InMemoryTaskAdapter())
                self.assertEqual("completed", migrated.manifest_status(manifest))
            finally:
                migrated.close()

    def test_atomic_json_file(self) -> None:
        add_approved(self.store)
        manifest = build_manifest(self.store)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "handoff.json"
            write_manifest_atomic(manifest, target)
            self.assertEqual(json.loads(target.read_text()), manifest)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_webhook_sender_and_secret_redaction(self) -> None:
        add_approved(self.store)
        manifest = build_manifest(self.store)
        adapter = WebhookTaskAdapter("https://tasks.example.test/handoff", lambda _url, payload: complete_receipt(payload))
        deliver_manifest(self.store, manifest, adapter)
        failing = WebhookTaskAdapter("https://tasks.example.test/handoff", lambda _url, _payload: (_ for _ in ()).throw(RuntimeError("Authorization: Bearer secret-value")))
        with self.assertRaisesRegex(RuntimeError, "REDACTED") as error:
            failing.upsert_manifest(manifest)
        self.assertNotIn("secret-value", str(error.exception))
        self.assertEqual(redact_external_error("api_key=abc123"), "api_key=[REDACTED]")


if __name__ == "__main__":
    unittest.main()
