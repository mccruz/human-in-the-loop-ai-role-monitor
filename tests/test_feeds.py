from __future__ import annotations

import json
from pathlib import Path
import unittest

from role_monitor.feeds import FeedRequest, next_feed_request, parse_feed, parse_generic_html, resolve_feed, validate_feed_payload


FIXTURES = Path(__file__).parents[1] / "examples" / "fixtures" / "ats"


class ResolveFeedTests(unittest.TestCase):
    def test_documented_vendor_endpoints(self) -> None:
        cases = {
            "https://boards.greenhouse.io/acme": ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"),
            "https://boards.eu.greenhouse.io/acme": ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"),
            "https://job-boards.eu.greenhouse.io/acme": ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"),
            "https://jobs.lever.co/acme": ("lever", "https://api.lever.co/v0/postings/acme?mode=json"),
            "https://jobs.eu.lever.co/acme": ("lever", "https://api.eu.lever.co/v0/postings/acme?mode=json"),
            "https://jobs.ashbyhq.com/acme": ("ashby", "https://api.ashbyhq.com/posting-api/job-board/acme"),
            "https://careers.smartrecruiters.com/acme": ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100&offset=0"),
            "https://apply.workable.com/acme": ("workable", "https://www.workable.com/api/accounts/acme?details=true"),
            "https://acme.recruitee.com": ("recruitee", "https://acme.recruitee.com/api/offers/"),
        }
        for careers_url, expected in cases.items():
            with self.subTest(careers_url=careers_url):
                request = resolve_feed(careers_url)
                self.assertEqual(request, FeedRequest(*expected))

    def test_rejects_lookalike_domains_and_bad_urls(self) -> None:
        for value in (
            "https://boards.greenhouse.io.attacker.example/acme",
            "https://evil-jobs.lever.co/acme",
            "https://acme.recruitee.com.attacker.example",
            "https://www.recruitee.com",
            "https://api.recruitee.com",
            "javascript:alert(1)",
            "https://jobs.lever.co/",
        ):
            with self.subTest(value=value):
                self.assertIsNone(resolve_feed(value))

    def test_smartrecruiters_pagination(self) -> None:
        request = resolve_feed("https://careers.smartrecruiters.com/acme")
        assert request is not None
        next_request = next_feed_request(request, {"offset": 0, "limit": 100, "totalFound": 250})
        self.assertEqual(
            next_request,
            FeedRequest("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100&offset=100"),
        )
        self.assertIsNone(next_feed_request(request, {"offset": 200, "limit": 100, "totalFound": 250}))


class NativeParserTests(unittest.TestCase):
    def test_each_fixture_parses_one_valid_job(self) -> None:
        for adapter in ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee"):
            with self.subTest(adapter=adapter):
                payload = json.loads((FIXTURES / f"{adapter}.json").read_text())
                jobs = parse_feed(adapter, payload, "Acme")
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0].source, adapter)
                self.assertTrue(jobs[0].url.startswith("https://"))

    def test_bad_and_duplicate_records_are_suppressed(self) -> None:
        payload = {"jobs": [
            {"id": "1", "title": "AI Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"},
            {"id": "different-id", "title": "AI Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1?source=board"},
            {"id": "2", "title": "No URL"},
        ]}
        self.assertEqual(len(parse_feed("greenhouse", payload, "Acme")), 1)

    def test_greenhouse_job_id_query_is_identity_not_tracking(self) -> None:
        payload = {"jobs": [
            {
                "id": "44444",
                "title": "Automation Engineer",
                "absolute_url": "https://acme.example/careers?gh_jid=44444",
            },
            {
                "id": "55555",
                "title": "AI Implementation Lead",
                "absolute_url": "https://acme.example/careers?gh_jid=55555",
            },
        ]}
        jobs = parse_feed("greenhouse", payload, "Acme")
        self.assertEqual(2, len(jobs))
        self.assertEqual(
            {
                "https://acme.example/careers?gh_jid=44444",
                "https://acme.example/careers?gh_jid=55555",
            },
            {job.as_dict()["url"] for job in jobs},
        )

    def test_empty_and_malformed_payloads_do_not_raise(self) -> None:
        self.assertEqual(parse_feed("greenhouse", None, "Acme"), [])
        self.assertEqual(parse_feed("lever", {"not": "a list"}, "Acme"), [])
        self.assertEqual(parse_feed("unknown", [], "Acme"), [])

    def test_each_native_adapter_rejects_a_provider_wrong_envelope(self) -> None:
        for adapter in ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee"):
            with self.subTest(adapter=adapter):
                with self.assertRaisesRegex(ValueError, "Invalid"):
                    validate_feed_payload(adapter, {"error": "not a feed"})

    def test_smartrecruiters_rejects_inconsistent_pagination(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty page"):
            validate_feed_payload("smartrecruiters", {
                "content": [], "offset": 0, "limit": 100, "totalFound": 1,
            })

    def test_nonempty_malformed_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "feed record"):
            validate_feed_payload("greenhouse", {"jobs": [{"unexpected": "shape"}]})

    def test_native_record_cannot_export_a_local_network_job_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "feed record"):
            validate_feed_payload("greenhouse", {"jobs": [{
                "id": "1", "title": "Automation Engineer",
                "absolute_url": "https://127.0.0.1/private-job",
            }]})

    def test_ashby_unlisted_role_is_not_surfaced(self) -> None:
        payload = {"jobs": [{
            "id": "direct-only",
            "title": "Unlisted Direct Link Role",
            "jobUrl": "https://jobs.ashbyhq.com/acme/direct-only",
            "isListed": False,
        }]}
        validate_feed_payload("ashby", payload)
        self.assertEqual([], parse_feed("ashby", payload, "Acme"))

    def test_smartrecruiters_prefers_public_apply_url(self) -> None:
        payload = json.loads((FIXTURES / "smartrecruiters.json").read_text())
        job = parse_feed("smartrecruiters", payload, "Acme")[0]
        self.assertEqual(job.url, "https://jobs.smartrecruiters.com/Acme/743999999999999-automation-consultant")

    def test_provider_specific_location_and_employment_fields_are_normalized(self) -> None:
        lever = parse_feed("lever", [{
            "id": "1",
            "text": "Implementation Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/1",
            "categories": {"location": "Manila", "commitment": "Full-time"},
        }], "Acme")[0]
        smartrecruiters = parse_feed("smartrecruiters", {"content": [{
            "id": "2",
            "name": "Automation Consultant",
            "company": {"identifier": "acme", "name": "Acme"},
            "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/2",
            "location": {"fullLocation": "Remote, Philippines", "remote": True},
            "typeOfEmployment": {"label": "Full-time"},
        }]}, "Acme")[0]
        workable = parse_feed("workable", {"jobs": [{
            "shortcode": "3",
            "title": "AI Implementation Lead",
            "url": "https://apply.workable.com/acme/j/3/",
            "location": {"location_str": "London, United Kingdom", "telecommuting": False},
        }]}, "Acme")[0]
        self.assertEqual((lever.location, lever.employment_type), ("Manila", "full-time"))
        self.assertEqual(lever.employment_type, "full-time")
        self.assertEqual(smartrecruiters.url, "https://jobs.smartrecruiters.com/acme/2")
        self.assertEqual((smartrecruiters.location, smartrecruiters.employment_type, smartrecruiters.remote), ("Remote, Philippines", "full-time", True))
        self.assertEqual((workable.location, workable.remote), ("London, United Kingdom", False))

    def test_employment_and_workplace_values_are_provider_neutral(self) -> None:
        lever = parse_feed("lever", [{
            "id": "1", "text": "Engineer", "hostedUrl": "https://jobs.lever.co/acme/1",
            "categories": {"commitment": "FullTime"}, "workplaceType": "hybrid",
        }], "Acme")[0]
        smart = parse_feed("smartrecruiters", {"content": [{
            "id": "2", "name": "Engineer", "applyUrl": "https://jobs.smartrecruiters.com/acme/2",
            "typeOfEmployment": {"label": "FULL_TIME"},
            "location": {"city": "Manila", "hybrid": True},
        }]}, "Acme")[0]
        workable = parse_feed("workable", {"jobs": [{
            "shortcode": "3", "title": "Engineer", "url": "https://apply.workable.com/acme/j/3/",
            "employment_type": "Full-time", "location": {"workplace_type": "hybrid"},
        }]}, "Acme")[0]
        self.assertEqual({"full-time"}, {lever.employment_type, smart.employment_type, workable.employment_type})
        self.assertEqual({"hybrid"}, {lever.workplace_type, smart.workplace_type, workable.workplace_type})
        self.assertFalse(lever.remote)

    def test_recruitee_live_shape_normalizes_employment_and_hybrid_flags(self) -> None:
        role = parse_feed("recruitee", {"offers": [{
            "id": "rc-1",
            "title": "Automation Engineer",
            "careers_url": "https://acme.recruitee.com/o/automation-engineer",
            "employment_type_code": "fulltime_permanent",
            "remote": False,
            "hybrid": True,
            "on_site": True,
        }]}, "Acme")[0]
        self.assertEqual("full-time", role.employment_type)
        self.assertEqual("hybrid", role.workplace_type)
        self.assertFalse(role.remote)


class GenericParserTests(unittest.TestCase):
    def test_json_ld_and_job_links_are_extracted_and_deduplicated(self) -> None:
        body = """
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"JobPosting","title":"AI Automation Engineer",
         "url":"/jobs/ai-automation","identifier":{"value":"role-1"},
         "hiringOrganization":{"name":"Acme"},"jobLocationType":"TELECOMMUTE"}
        </script>
        <a href="/jobs/ai-automation">AI Automation Engineer</a>
        <a href="javascript:alert(1)">Bad job</a>
        """
        jobs = parse_generic_html(body, "https://careers.example/jobs", "Fallback Co")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "Acme")
        self.assertTrue(jobs[0].remote)

    def test_malformed_json_ld_and_irrelevant_links_are_safe(self) -> None:
        jobs = parse_generic_html(
            '<script type="application/ld+json">{not json</script><a href="/about">About</a>',
            "https://careers.example/",
            "Acme",
        )
        self.assertEqual(jobs, [])

    def test_json_ld_list_item_nodes_are_walked(self) -> None:
        body = '''<script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"@type":"ListItem","item":{"@type":"JobPosting","title":"AI Engineer","url":"/jobs/1"}}]}</script>'''
        jobs = parse_generic_html(body, "https://careers.example/", "Acme")
        self.assertEqual([job.title for job in jobs], ["AI Engineer"])

    def test_json_ld_place_postal_address_is_normalized(self) -> None:
        body = '''<script type="application/ld+json">{"@type":"JobPosting","title":"Automation Engineer","url":"/jobs/1","jobLocation":{"@type":"Place","address":{"@type":"PostalAddress","addressLocality":"Manila","addressRegion":"NCR","addressCountry":"PH"}}}</script>'''
        job = parse_generic_html(body, "https://careers.example/", "Acme")[0]
        self.assertEqual("Manila, NCR, PH", job.location)


if __name__ == "__main__":
    unittest.main()
