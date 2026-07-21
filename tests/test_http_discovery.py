from __future__ import annotations

import unittest

from role_monitor.discovery import discover_all, discover_one
from role_monitor.feeds import resolve_feed
from role_monitor.http import HttpResponse, MappingTransport, RetryPolicy, fetch_with_retry, json_response, validate_public_https_url
from role_monitor.models import Employer


class HttpAndDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.retry = RetryPolicy(attempts=3, timeout_seconds=1, backoff_seconds=0)

    def test_transient_status_retries_then_succeeds(self):
        url = "https://example.test/feed"
        transport = MappingTransport({url: [
            HttpResponse(503, url, ""),
            json_response(url, {"ok": True}),
        ]})
        result = fetch_with_retry(transport, url, self.retry, user_agent="test", sleep=lambda _: None)
        self.assertEqual(2, result.attempts)
        self.assertEqual(200, result.response.status)

    def test_live_transport_target_validation_rejects_local_networks(self):
        for value in (
            "https://127.0.0.1/jobs",
            "https://[::1]/jobs",
            "https://localhost/jobs",
            "http://example.com/jobs",
            "https://user:password@example.com/jobs",
        ):
            with self.subTest(value=value):
                with self.assertRaises((ValueError, OSError)):
                    validate_public_https_url(value, resolve_dns=False)

    def test_terminal_client_error_is_not_retried(self):
        url = "https://example.test/missing"
        transport = MappingTransport({url: HttpResponse(404, url, "")})
        result = fetch_with_retry(transport, url, self.retry, user_agent="test", sleep=lambda _: None)
        self.assertEqual(1, result.attempts)
        self.assertEqual(1, transport.calls[url])

    def test_native_discovery_parses_jobs(self):
        employer = Employer("greenhouse", "Northstar Automation", "https://boards.greenhouse.io/northstar-demo")
        request = resolve_feed(employer.careers_url)
        assert request is not None
        transport = MappingTransport({request.url: json_response(request.url, {"jobs": [{
            "id": 1,
            "title": "AI Automation Specialist",
            "absolute_url": "https://boards.greenhouse.io/northstar-demo/jobs/1",
        }]})})
        result = discover_one(employer, transport, self.retry)
        self.assertTrue(result.ok)
        self.assertTrue(result.complete)
        self.assertEqual("AI Automation Specialist", result.jobs[0].title)

    def test_provider_wrong_success_body_is_a_source_failure(self):
        employer = Employer("greenhouse", "Northstar Automation", "https://boards.greenhouse.io/northstar-demo")
        request = resolve_feed(employer.careers_url)
        assert request is not None
        result = discover_one(
            employer,
            MappingTransport({request.url: json_response(request.url, {"error": "wrong envelope"})}),
            self.retry,
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.complete)
        self.assertIn("Invalid greenhouse feed envelope", result.failure.error)

    def test_nonempty_schema_drift_is_failure_not_a_complete_empty_scan(self):
        employer = Employer("greenhouse", "Northstar Automation", "https://boards.greenhouse.io/northstar-demo")
        request = resolve_feed(employer.careers_url)
        assert request is not None
        result = discover_one(
            employer,
            MappingTransport({request.url: json_response(request.url, {"jobs": [{"unexpected": "shape"}]})}),
            self.retry,
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.complete)
        self.assertIn("Invalid greenhouse feed record", result.failure.error)

    def test_one_source_failure_does_not_abort_other_sources(self):
        good = Employer("good", "Good Automation", "https://jobs.lever.co/good-demo")
        bad = Employer("bad", "Bad Automation", "https://jobs.ashbyhq.com/bad-demo")
        good_request = resolve_feed(good.careers_url)
        bad_request = resolve_feed(bad.careers_url)
        assert good_request is not None and bad_request is not None
        transport = MappingTransport({
            good_request.url: json_response(good_request.url, [{
                "id": "1",
                "text": "Workflow Implementation Engineer",
                "hostedUrl": "https://jobs.lever.co/good-demo/1",
            }]),
            bad_request.url: RuntimeError("Authorization: Bearer portfolio-canary"),
        })
        results = discover_all([good, bad], transport, RetryPolicy(attempts=1, timeout_seconds=1, backoff_seconds=0))
        self.assertEqual(["good", "bad"], [result.employer.key for result in results])
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertNotIn("portfolio-canary", results[1].failure.error)

    def test_smartrecruiters_pages_are_combined_and_deduplicated(self):
        employer = Employer("smart", "Beacon Workflow", "https://careers.smartrecruiters.com/BeaconDemo")
        first = resolve_feed(employer.careers_url)
        assert first is not None
        second_url = first.url.replace("offset=0", "offset=100")
        duplicate = {
            "id": "one",
            "name": "AI Automation Consultant",
            "applyUrl": "https://jobs.smartrecruiters.com/BeaconDemo/one",
        }
        transport = MappingTransport({
            first.url: json_response(first.url, {
                "offset": 0,
                "limit": 100,
                "totalFound": 101,
                "content": [duplicate],
            }),
            second_url: json_response(second_url, {
                "offset": 100,
                "limit": 100,
                "totalFound": 101,
                "content": [duplicate, {
                    "id": "two",
                    "name": "Workflow Implementation Specialist",
                    "applyUrl": "https://jobs.smartrecruiters.com/BeaconDemo/two",
                }],
            }),
        })
        result = discover_one(employer, transport, self.retry)
        self.assertTrue(result.ok)
        self.assertEqual(2, len(result.jobs))
        self.assertEqual(2, result.attempts)
        self.assertEqual(1, transport.calls[second_url])

    def test_two_boards_with_same_company_label_and_local_id_do_not_collide(self):
        employers = [
            Employer("board-a", "Example", "https://boards.greenhouse.io/board-a"),
            Employer("board-b", "Example", "https://boards.greenhouse.io/board-b"),
        ]
        responses = {}
        for employer in employers:
            request = resolve_feed(employer.careers_url)
            assert request is not None
            responses[request.url] = json_response(request.url, {"jobs": [{
                "id": "local-1",
                "title": "Automation Engineer",
                "absolute_url": f"https://boards.greenhouse.io/{employer.key}/jobs/local-1",
            }]})
        results = discover_all(employers, MappingTransport(responses), self.retry)
        self.assertNotEqual(results[0].jobs[0].role_key, results[1].jobs[0].role_key)

    def test_smartrecruiters_summary_is_hydrated_from_trusted_detail_ref(self):
        employer = Employer("smart", "Beacon Workflow", "https://careers.smartrecruiters.com/BeaconDemo")
        request = resolve_feed(employer.careers_url)
        assert request is not None
        detail_url = "https://api.smartrecruiters.com/api-v1/companies/BeaconDemo/postings/one"
        transport = MappingTransport({
            request.url: json_response(request.url, {
                "offset": 0,
                "limit": 100,
                "totalFound": 1,
                "content": [{
                    "id": "one",
                    "name": "Automation Consultant",
                    "company": {"identifier": "BeaconDemo"},
                    "ref": detail_url,
                    "location": {"city": "Manila", "remote": True},
                }],
            }),
            detail_url: json_response(detail_url, {
                "id": "one",
                "name": "Automation Consultant",
                "company": {"identifier": "BeaconDemo"},
                "postingUrl": "https://jobs.smartrecruiters.com/BeaconDemo/one",
                "location": {"city": "Manila", "remote": True},
                "jobAd": {"sections": {
                    "jobDescription": {"text": "Build AI workflow automation."},
                    "qualifications": {"text": "Implementation experience."},
                }},
            }),
        })
        result = discover_one(employer, transport, self.retry)
        self.assertTrue(result.ok)
        self.assertEqual(2, result.attempts)
        self.assertIn("AI workflow automation", result.jobs[0].description)
        self.assertEqual("https://jobs.smartrecruiters.com/BeaconDemo/one", result.jobs[0].url)

    def test_smartrecruiters_wrong_detail_object_fails_the_source(self):
        employer = Employer("smart", "Beacon Workflow", "https://careers.smartrecruiters.com/BeaconDemo")
        request = resolve_feed(employer.careers_url)
        assert request is not None
        detail_url = "https://api.smartrecruiters.com/v1/companies/BeaconDemo/postings/one"
        transport = MappingTransport({
            request.url: json_response(request.url, {
                "offset": 0, "limit": 100, "totalFound": 1,
                "content": [{
                    "id": "one", "name": "Automation Consultant",
                    "company": {"identifier": "BeaconDemo"}, "ref": detail_url,
                }],
            }),
            detail_url: json_response(detail_url, {
                "id": "different", "name": "Wrong Job",
                "company": {"identifier": "OtherCompany"},
                "postingUrl": "https://jobs.smartrecruiters.com/OtherCompany/different",
            }),
        })
        result = discover_one(employer, transport, self.retry)
        self.assertFalse(result.ok)
        self.assertIn("does not match", result.failure.error)


if __name__ == "__main__":
    unittest.main()
