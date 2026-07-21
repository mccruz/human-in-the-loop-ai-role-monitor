from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from role_monitor.cli import main
from role_monitor.handoff import complete_receipt
from role_monitor.store import RoleStore


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(arguments)
        return status, json.loads(output.getvalue())

    def test_scan_requires_explicit_network_opt_in_before_transport_creation(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["scan", "--config", str(ROOT / "config.example.json")])
        self.assertEqual(2, error.exception.code)

    def test_offline_demo_writes_manifest_and_receipt_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            status, result = self.invoke([
                "demo", "--config", str(ROOT / "config.example.json"), "--output-dir", directory,
            ])
            self.assertEqual(0, status)
            self.assertEqual("offline synthetic demo", result["mode"])
            self.assertEqual("complete", result["receipt"]["status"])
            self.assertEqual(1, result["final_state"]["delivered"])
            self.assertTrue(Path(str(result["manifest_path"])).exists())
            self.assertTrue(Path(str(result["receipt_path"])).exists())

    def test_queue_review_and_prepare_handoff_use_explicit_local_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo_roles.sqlite3"
            _, demo = self.invoke([
                "demo", "--config", str(ROOT / "config.example.json"), "--output-dir", directory,
            ])
            self.assertTrue(demo["approved_role_keys"])
            status, queue = self.invoke([
                "queue", "--config", str(ROOT / "config.example.json"), "--database", str(database),
            ])
            self.assertEqual(0, status)
            role_key = queue["pending"][0]["role_key"]
            status, reviewed = self.invoke([
                "review", "--config", str(ROOT / "config.example.json"), "--database", str(database),
                role_key, "approved", "--note", "reviewed by test",
            ])
            self.assertEqual(0, status)
            self.assertEqual("approved", reviewed["review_state"])
            destination = Path(directory) / "prepared.json"
            status, prepared = self.invoke([
                "prepare-handoff", "--config", str(ROOT / "config.example.json"), "--database", str(database),
                "--destination", str(destination),
            ])
            self.assertEqual(0, status)
            self.assertTrue(destination.exists())
            self.assertEqual(prepared["manifest"]["manifest_id"], json.loads(destination.read_text())["manifest_id"])
            receipt = Path(directory) / "receipt.json"
            receipt.write_text(json.dumps(complete_receipt(prepared["manifest"])), encoding="utf-8")
            status, acknowledged = self.invoke([
                "acknowledge-receipt", "--config", str(ROOT / "config.example.json"),
                "--database", str(database), "--manifest", str(destination), "--receipt", str(receipt),
            ])
            self.assertEqual(0, status)
            self.assertEqual("verified and recorded", acknowledged["status"])
            _, state = self.invoke([
                "queue", "--config", str(ROOT / "config.example.json"), "--database", str(database),
            ])
            self.assertEqual(2, state["counts"]["delivered"])

    def test_demo_requires_explicit_reset_before_replacing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = ["demo", "--config", str(ROOT / "config.example.json"), "--output-dir", directory]
            self.invoke(arguments)
            with self.assertRaisesRegex(FileExistsError, "--reset"):
                self.invoke(arguments)
            status, result = self.invoke([*arguments, "--reset"])
            self.assertEqual(0, status)
            self.assertEqual("complete", result["receipt"]["status"])

    def test_recover_handoff_requires_explicit_reconciliation_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "roles.sqlite3"
            store = RoleStore(database)
            try:
                from role_monitor.models import JobPosting, ScoredPosting
                from role_monitor.review import ReviewService
                from role_monitor.handoff import build_manifest, write_manifest_atomic

                row = store.upsert_scored(ScoredPosting(
                    JobPosting("lever", "recover", "Example", "AI Automation Engineer", "https://jobs.example.test/recover"),
                    90,
                    "High",
                    ("+35 AI automation",),
                ))
                ReviewService(store).approve(row["role_key"])
                manifest = build_manifest(store)
                manifest_path = write_manifest_atomic(manifest, Path(directory) / "manifest.json")
                store.claim_manifest(manifest)
            finally:
                store.close()

            arguments = [
                "recover-handoff", "--config", str(ROOT / "config.example.json"),
                "--database", str(database), "--manifest", str(manifest_path),
            ]
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(arguments)
            self.assertEqual(2, error.exception.code)
            status, result = self.invoke([*arguments, "--confirm-downstream-reconciled"])
            self.assertEqual(0, status)
            self.assertEqual("released for idempotent retry", result["status"])


if __name__ == "__main__":
    unittest.main()
