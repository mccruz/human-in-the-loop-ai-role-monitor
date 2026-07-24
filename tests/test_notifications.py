from __future__ import annotations

import unittest

from role_monitor.notifications import (
    MAX_NOTIFICATION_TEXT,
    DryRunNotifier,
    Notification,
    build_scan_notification,
)


def summary(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "generated_at": "2030-01-02T03:04:05+00:00",
        "sources_attempted": 6,
        "sources_succeeded": 5,
        "sources_failed": 1,
        "postings_discovered": 12,
        "new_role_records": 3,
        "review_queue": 4,
        "approved_undelivered": 2,
    }
    result.update(overrides)
    return result


class NotificationTests(unittest.TestCase):
    def test_scan_template_is_deterministic_and_summary_only(self):
        first = build_scan_notification({
            **summary(),
            "review_note": "private reviewer rationale",
            "database": "/private/production/path.sqlite3",
            "token": "portfolio-canary",
        })
        second = build_scan_notification(summary())
        self.assertEqual(second, first)
        self.assertIn("Sources: 5/6 succeeded; 1 failed", first.text)
        self.assertIn("Human review remains required", first.text)
        self.assertNotIn("private reviewer rationale", first.text)
        self.assertNotIn("/private/production", first.text)
        self.assertNotIn("portfolio-canary", first.text)

    def test_scan_id_changes_when_allowlisted_evidence_changes(self):
        first = build_scan_notification(summary())
        second = build_scan_notification(summary(new_role_records=4))
        self.assertNotEqual(first.text.splitlines()[1], second.text.splitlines()[1])

    def test_invalid_summary_counts_are_rejected(self):
        for value in (True, -1, "not-a-number", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "new_role_records"):
                    build_scan_notification(summary(new_role_records=value))

    def test_generated_timestamp_cannot_carry_arbitrary_text(self):
        with self.assertRaisesRegex(ValueError, "ISO-8601"):
            build_scan_notification(summary(generated_at="token=portfolio-canary"))
        with self.assertRaisesRegex(ValueError, "timezone"):
            build_scan_notification(summary(generated_at="2030-01-02T03:04:05"))

    def test_notification_requires_bounded_plain_text(self):
        accepted = Notification("scan.completed", "x" * MAX_NOTIFICATION_TEXT)
        self.assertEqual(MAX_NOTIFICATION_TEXT, len(accepted.text))
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            Notification("scan.completed", "x" * (MAX_NOTIFICATION_TEXT + 1))
        with self.assertRaisesRegex(ValueError, "text is required"):
            Notification("scan.completed", "  ")

    def test_dry_run_returns_preview_without_attempting_delivery(self):
        notification = build_scan_notification(summary())
        receipt = DryRunNotifier("telegram").send(notification)
        self.assertEqual({
            "provider": "telegram",
            "status": "dry-run",
            "attempts": 0,
            "preview": notification.text,
        }, receipt.as_dict())


if __name__ == "__main__":
    unittest.main()
