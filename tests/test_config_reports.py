from __future__ import annotations

import json
import csv
from pathlib import Path
import stat
import tempfile
import unittest

from role_monitor.config import load_config
from role_monitor.reports import export_reports


ROOT = Path(__file__).resolve().parents[1]


class ConfigAndReportTests(unittest.TestCase):
    def test_example_config_is_valid_and_has_six_sources(self):
        config = load_config(ROOT / "config.example.json")
        self.assertEqual(6, len(config.employers))
        self.assertEqual(6, config.concurrency)

    def test_authentication_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            payload = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
            payload["api_token"] = "portfolio-canary"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Authentication field"):
                load_config(path)

    def test_nested_credential_shaped_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            payload = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
            payload["delivery"] = {"authorization_header": "placeholder"}
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Authentication field"):
                load_config(path)

    def test_employer_keys_are_case_insensitively_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            payload = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
            payload["employers"] = [
                {"key": "Acme", "name": "Example", "careers_url": "https://boards.greenhouse.io/acme-a"},
                {"key": "acme", "name": "Example", "careers_url": "https://boards.greenhouse.io/acme-b"},
            ]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "regardless of letter case"):
                load_config(path)

    def test_reports_are_atomic_sorted_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            paths = export_reports(
                output,
                jobs=[{"role_key": "role-b"}, {"role_key": "role-a"}],
                review_queue=[{
                    "role_key": "role-a",
                    "company": "Example",
                    "title": "AI Automation Specialist",
                    "url": "https://example.test/jobs/1?token=portfolio-canary",
                    "location": "Remote",
                    "score": 80,
                    "band": "High",
                    "review_state": "pending",
                }],
                failures=[{"employer_key": "example", "error": "Authorization: Bearer portfolio-canary"}],
                summary={"attempted": 1, "succeeded": 0},
            )
            jobs = json.loads(Path(paths["jobs"]).read_text(encoding="utf-8"))
            self.assertEqual(["role-a", "role-b"], [row["role_key"] for row in jobs])
            combined = "".join(path.read_text(encoding="utf-8") for path in output.iterdir())
            self.assertNotIn("portfolio-canary", combined)
            self.assertFalse(list(output.glob("*.tmp")))
            for path in output.iterdir():
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_csv_neutralizes_formula_cells_and_omits_private_review_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = export_reports(
                Path(directory),
                jobs=[{
                    "role_key": "role-a",
                    "title": "=HYPERLINK(\"https://attacker.example\")",
                    "review_note": "private reviewer rationale",
                }],
                review_queue=[{
                    "role_key": "role-a",
                    "company": "Example",
                    "title": "=HYPERLINK(\"https://attacker.example\")",
                    "url": "https://example.test/jobs/1",
                    "location": "Remote",
                    "score": 80,
                    "band": "High",
                    "review_state": "pending",
                    "review_note": "private reviewer rationale",
                }],
                failures=[],
                summary={},
            )
            with Path(paths["review_queue"]).open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertTrue(row["title"].startswith("'="))
            combined = "".join(path.read_text(encoding="utf-8") for path in Path(directory).iterdir())
            self.assertNotIn("private reviewer rationale", combined)

    def test_report_writes_do_not_follow_predictable_temp_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim.txt"
            victim.write_text("preserve me", encoding="utf-8")
            (root / "scan_summary.json.tmp").symlink_to(victim)
            export_reports(root, jobs=[], review_queue=[], failures=[], summary={"ok": True})
            self.assertEqual("preserve me", victim.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
