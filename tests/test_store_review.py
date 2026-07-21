from __future__ import annotations

import unittest
from pathlib import Path
import stat
import sqlite3
import tempfile

from role_monitor.models import JobPosting, ScoredPosting
from role_monitor.review import ReviewService
from role_monitor.store import RoleStore


def scored(identifier: str, score: int = 70) -> ScoredPosting:
    return ScoredPosting(
        posting=JobPosting("greenhouse", identifier, "Example Co", f"AI Engineer {identifier}", f"https://jobs.example.test/{identifier}?utm_source=test"),
        score=score,
        band="strong",
        reasons=("automation",),
    )


class StoreAndReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RoleStore()
        self.review = ReviewService(self.store)

    def tearDown(self) -> None:
        self.store.close()

    def test_duplicate_discovery_preserves_human_decision(self) -> None:
        first = self.store.upsert_scored(scored("one", 70))
        self.review.approve(first["role_key"], "reviewed")
        updated = self.store.upsert_scored(scored("one", 95))
        self.assertEqual(updated["review_state"], "approved")
        self.assertEqual(updated["review_note"], "reviewed")
        self.assertEqual(updated["score"], 95)
        self.assertEqual([event["event_type"] for event in self.store.audit_events(first["role_key"])], ["discovery_upserted", "review_decided", "discovery_upserted"])

    def test_pending_queue_is_deterministic_and_deferred_is_separate(self) -> None:
        low = self.store.upsert_scored(scored("low", 10))
        high = self.store.upsert_scored(scored("high", 90))
        self.review.defer(low["role_key"], "later")
        self.assertEqual([row["role_key"] for row in self.review.queue()], [high["role_key"]])
        self.assertEqual([row["role_key"] for row in self.review.deferred()], [low["role_key"]])

    def test_invalid_transitions_and_inputs_are_rejected(self) -> None:
        record = self.store.upsert_scored(scored("one"))
        with self.assertRaises(ValueError):
            self.store.decide(record["role_key"], "pending")
        self.review.reject(record["role_key"])
        with self.assertRaises(ValueError):
            self.review.approve(record["role_key"])
        with self.assertRaises(KeyError):
            self.review.approve("role-missing")

    def test_only_approved_records_are_handoff_eligible(self) -> None:
        approved = self.store.upsert_scored(scored("approved"))
        self.store.upsert_scored(scored("pending"))
        self.review.approve(approved["role_key"])
        self.assertEqual([row["role_key"] for row in self.store.approved_undelivered()], [approved["role_key"]])

    def test_below_threshold_role_is_excluded_and_can_reenter_queue(self) -> None:
        low = self.store.upsert_scored(scored("threshold", 10), reviewable=False)
        self.assertEqual("excluded", low["review_state"])
        self.assertEqual([], self.review.queue())
        with self.assertRaisesRegex(ValueError, "excluded"):
            self.review.approve(low["role_key"])
        promoted = self.store.upsert_scored(scored("threshold", 80), reviewable=True)
        self.assertEqual("pending", promoted["review_state"])

    def test_rescore_cannot_reset_final_human_decision(self) -> None:
        record = self.store.upsert_scored(scored("final", 80), reviewable=True)
        self.review.reject(record["role_key"], "not a fit")
        rescored = self.store.upsert_scored(scored("final", 5), reviewable=False)
        self.assertEqual("rejected", rescored["review_state"])
        self.assertEqual("not a fit", rescored["review_note"])

    def test_file_database_is_private_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.sqlite3"
            store = RoleStore(path)
            store.close()
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_legacy_approval_without_snapshot_requires_re_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.sqlite3"
            store = RoleStore(path)
            record = store.upsert_scored(scored("legacy"))
            store.decide(record["role_key"], "approved", "old approval")
            store._connection.execute(
                "DELETE FROM approval_snapshots WHERE role_key=?", (record["role_key"],)
            )
            store._connection.execute(
                "UPDATE roles SET title='Changed after old approval' WHERE role_key=?",
                (record["role_key"],),
            )
            store._connection.commit()
            store.close()
            reopened = RoleStore(path)
            try:
                row = reopened.get(record["role_key"])
                self.assertEqual("pending", row["review_state"])
                self.assertIn("no immutable snapshot", row["review_note"])
                self.assertEqual([], reopened.approved_undelivered())
            finally:
                reopened.close()

    def test_legacy_role_without_employer_identity_is_inactive_until_rediscovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE roles (
                    role_key TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                    company TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    location TEXT NOT NULL, description TEXT NOT NULL,
                    employment_type TEXT NOT NULL, remote INTEGER, score INTEGER NOT NULL,
                    band TEXT NOT NULL, reasons_json TEXT NOT NULL, discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    review_state TEXT NOT NULL CHECK (review_state IN ('pending','approved','rejected','deferred','excluded')),
                    review_note TEXT NOT NULL DEFAULT '', review_decided_at TEXT,
                    delivered_at TEXT, delivery_manifest_id TEXT
                );
                INSERT INTO roles VALUES (
                    'role-legacy','greenhouse','1','Example','Automation Engineer',
                    'https://jobs.example.test/1','','','',NULL,80,'High','[]',
                    '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',
                    'pending','',NULL,NULL,NULL
                );
                """
            )
            connection.close()
            migrated = RoleStore(path)
            try:
                row = migrated.get("role-legacy")
                self.assertFalse(row["active"])
                self.assertEqual([], migrated.pending_review())
                with self.assertRaisesRegex(ValueError, "inactive"):
                    migrated.decide("role-legacy", "approved")
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()
