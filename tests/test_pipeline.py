from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from role_monitor.config import load_config
from role_monitor.demo import SYNTHETIC_DECISION_NOTE, run_demo
from role_monitor.feeds import resolve_feed
from role_monitor.handoff import build_manifest
from role_monitor.http import MappingTransport, json_response
from role_monitor.pipeline import run_pipeline
from role_monitor.store import RoleStore


ROOT = Path(__file__).resolve().parents[1]


def fixture_responses(config, *, greenhouse: object | None = None):
    fixtures = ROOT / "examples" / "fixtures" / "ats"
    responses = {}
    for employer in config.employers:
        request = resolve_feed(employer.careers_url)
        assert request is not None
        if request.adapter == "greenhouse" and greenhouse is not None:
            value = greenhouse
        else:
            payload = json.loads((fixtures / f"{request.adapter}.json").read_text(encoding="utf-8"))
            value = json_response(request.url, payload)
        responses[request.url] = value
    return responses


class PipelineTests(unittest.TestCase):
    def test_pipeline_scores_persists_reports_and_preserves_decisions(self):
        config = load_config(ROOT / "config.example.json")
        fixtures = ROOT / "examples" / "fixtures" / "ats"
        responses = {}
        for employer in config.employers:
            request = resolve_feed(employer.careers_url)
            assert request is not None
            payload = json.loads((fixtures / f"{request.adapter}.json").read_text(encoding="utf-8"))
            responses[request.url] = json_response(request.url, payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RoleStore(root / "roles.sqlite3")
            try:
                first = run_pipeline(config, MappingTransport(responses), store, root / "reports")
                self.assertEqual(6, first.summary["sources_succeeded"])
                self.assertEqual(6, first.summary["postings_discovered"])
                self.assertGreater(first.summary["review_queue"], 0)
                chosen = store.pending_review()[0]
                store.decide(chosen["role_key"], "approved", "synthetic reviewer decision")

                second_responses = {}
                for employer in config.employers:
                    request = resolve_feed(employer.careers_url)
                    assert request is not None
                    payload = json.loads((fixtures / f"{request.adapter}.json").read_text(encoding="utf-8"))
                    second_responses[request.url] = json_response(request.url, payload)
                second = run_pipeline(config, MappingTransport(second_responses), store, root / "reports")
                self.assertEqual(0, second.summary["new_role_records"])
                self.assertEqual("approved", store.get(chosen["role_key"])["review_state"])
                self.assertTrue(Path(second.reports["summary"]).exists())
            finally:
                store.close()

    def test_offline_demo_uses_fixtures_and_marks_its_synthetic_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            demo = run_demo(ROOT / "config.example.json", root / "demo")
            self.assertEqual(6, demo.pipeline.summary["sources_succeeded"])
            self.assertEqual(1, demo.task_count)
            self.assertEqual("complete", demo.receipt["status"])
            self.assertEqual(1, demo.final_state["delivered"])
            self.assertTrue(Path(demo.manifest_path).exists())
            store = RoleStore(root / "demo" / "demo_roles.sqlite3")
            try:
                row = store.get(demo.approved_role_keys[0])
                self.assertEqual(SYNTHETIC_DECISION_NOTE, row["review_note"])
                self.assertIsNotNone(row["delivered_at"])
            finally:
                store.close()

    def test_complete_source_scan_retires_missing_role_and_invalidates_manifest(self):
        config = load_config(ROOT / "config.example.json")
        with tempfile.TemporaryDirectory() as directory:
            store = RoleStore(Path(directory) / "roles.sqlite3")
            try:
                run_pipeline(config, MappingTransport(fixture_responses(config)), store, Path(directory) / "reports")
                role = next(row for row in store.all_roles() if row["source"] == "greenhouse")
                store.decide(role["role_key"], "approved", "reviewed before closure")
                manifest = build_manifest(store)
                request = resolve_feed(next(item.careers_url for item in config.employers if item.key == "greenhouse-demo"))
                assert request is not None
                first_missing = run_pipeline(
                    config,
                    MappingTransport(fixture_responses(config, greenhouse=json_response(request.url, {"jobs": []}))),
                    store,
                    Path(directory) / "reports",
                )
                paused = store.get(role["role_key"])
                self.assertEqual(0, first_missing.summary["roles_retired"])
                self.assertTrue(paused["active"])
                self.assertEqual(1, paused["missing_complete_scans"])
                self.assertEqual("approved", paused["review_state"])
                self.assertEqual("invalidated", store.manifest_status(manifest))
                self.assertEqual([], store.approved_undelivered())
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    store.register_manifest(manifest)

                second_missing = run_pipeline(
                    config,
                    MappingTransport(fixture_responses(config, greenhouse=json_response(request.url, {"jobs": []}))),
                    store,
                    Path(directory) / "reports",
                )
                retired = store.get(role["role_key"])
                self.assertEqual(1, second_missing.summary["roles_retired"])
                self.assertFalse(retired["active"])
                self.assertEqual("pending", retired["review_state"])
                self.assertEqual("invalidated", store.manifest_status(manifest))
                self.assertEqual([], store.approved_undelivered())
                run_pipeline(
                    config,
                    MappingTransport(fixture_responses(config)),
                    store,
                    Path(directory) / "reports",
                )
                reappeared = store.get(role["role_key"])
                self.assertTrue(reappeared["active"])
                self.assertEqual("pending", reappeared["review_state"])
                store.decide(role["role_key"], "approved", "reviewed again after reappearing")
                replacement = build_manifest(store)
                self.assertNotEqual(manifest["manifest_id"], replacement["manifest_id"])
                self.assertEqual("pending", store.manifest_status(replacement))
            finally:
                store.close()

    def test_one_empty_complete_scan_then_recovery_preserves_approval(self):
        config = load_config(ROOT / "config.example.json")
        with tempfile.TemporaryDirectory() as directory:
            store = RoleStore(Path(directory) / "roles.sqlite3")
            try:
                run_pipeline(
                    config,
                    MappingTransport(fixture_responses(config)),
                    store,
                    Path(directory) / "reports",
                )
                role = next(row for row in store.all_roles() if row["source"] == "greenhouse")
                store.decide(role["role_key"], "approved", "reviewed")
                manifest = build_manifest(store)
                request = resolve_feed(
                    next(
                        item.careers_url
                        for item in config.employers
                        if item.key == "greenhouse-demo"
                    )
                )
                assert request is not None
                run_pipeline(
                    config,
                    MappingTransport(
                        fixture_responses(
                            config,
                            greenhouse=json_response(request.url, {"jobs": []}),
                        )
                    ),
                    store,
                    Path(directory) / "reports",
                )
                run_pipeline(
                    config,
                    MappingTransport(fixture_responses(config)),
                    store,
                    Path(directory) / "reports",
                )
                recovered = store.get(role["role_key"])
                self.assertTrue(recovered["active"])
                self.assertEqual(0, recovered["missing_complete_scans"])
                self.assertEqual("approved", recovered["review_state"])
                reactivated = build_manifest(store)
                self.assertEqual(manifest["manifest_id"], reactivated["manifest_id"])
                self.assertEqual("pending", store.manifest_status(reactivated))
            finally:
                store.close()

    def test_source_failure_does_not_retire_or_unapprove_role(self):
        config = load_config(ROOT / "config.example.json")
        with tempfile.TemporaryDirectory() as directory:
            store = RoleStore(Path(directory) / "roles.sqlite3")
            try:
                run_pipeline(config, MappingTransport(fixture_responses(config)), store, Path(directory) / "reports")
                role = next(row for row in store.all_roles() if row["source"] == "greenhouse")
                store.decide(role["role_key"], "approved", "reviewed")
                second = run_pipeline(
                    config,
                    MappingTransport(fixture_responses(config, greenhouse=RuntimeError("source unavailable"))),
                    store,
                    Path(directory) / "reports",
                )
                preserved = store.get(role["role_key"])
                self.assertEqual(1, second.summary["sources_failed"])
                self.assertEqual(0, second.summary["roles_retired"])
                self.assertTrue(preserved["active"])
                self.assertEqual("approved", preserved["review_state"])
                self.assertEqual(role["role_key"], build_manifest(store)["roles"][0]["role_key"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
