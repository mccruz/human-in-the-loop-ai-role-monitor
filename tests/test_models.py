from __future__ import annotations

import unittest

from role_monitor.models import Employer, JobPosting, canonical_url


class ModelIdentityTests(unittest.TestCase):
    def test_tracking_parameters_are_removed(self):
        self.assertEqual(
            "https://jobs.example.test/openings/42?language=en",
            canonical_url("HTTPS://JOBS.EXAMPLE.TEST/openings/42/?utm_source=board&language=en#apply"),
        )

    def test_greenhouse_job_id_query_is_preserved(self):
        self.assertEqual(
            "https://example.test/careers?gh_jid=44444",
            canonical_url(
                "https://example.test/careers?gh_jid=44444&gh_src=linkedin&utm_source=board"
            ),
        )

    def test_stable_external_id_survives_title_and_url_presentation_changes(self):
        first = JobPosting("greenhouse", "42", "Example", "Automation Engineer", "https://boards.greenhouse.io/example/jobs/42")
        second = JobPosting("greenhouse", "42", "Example", "Senior Automation Engineer", "https://job-boards.greenhouse.io/example/jobs/42?utm_source=careers")
        self.assertEqual(first.role_key, second.role_key)

    def test_fallback_identity_uses_canonical_url_not_title(self):
        first = JobPosting("generic", "", "Example", "Automation Engineer", "https://example.test/jobs/42?source=board")
        second = JobPosting("generic", "", "Example", "Implementation Engineer", "https://example.test/jobs/42")
        self.assertEqual(first.role_key, second.role_key)

    def test_tenant_boundary_prevents_cross_company_id_collision(self):
        first = JobPosting("greenhouse", "42", "Example A", "Automation Engineer", "https://example.test/a/42")
        second = JobPosting("greenhouse", "42", "Example B", "Automation Engineer", "https://example.test/b/42")
        self.assertNotEqual(first.role_key, second.role_key)

    def test_employer_key_prevents_same_label_board_id_collision(self):
        first = JobPosting(
            "greenhouse", "42", "Example", "Automation Engineer",
            "https://example.test/a/42", employer_key="board-a",
        )
        second = JobPosting(
            "greenhouse", "42", "Example", "Automation Engineer",
            "https://example.test/b/42", employer_key="board-b",
        )
        self.assertNotEqual(first.role_key, second.role_key)

    def test_urls_reject_credentials_insecure_scheme_and_private_employer_ip(self):
        for value in (
            "http://example.test/jobs/1",
            "https://user:password@example.test/jobs/1",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    canonical_url(value)
        with self.assertRaisesRegex(ValueError, "non-public"):
            Employer("local", "Local", "https://127.0.0.1/jobs")

    def test_ipv6_authority_remains_bracketed(self):
        self.assertEqual(
            "https://[2001:4860:4860::8888]/jobs",
            canonical_url("https://[2001:4860:4860::8888]/jobs"),
        )


if __name__ == "__main__":
    unittest.main()
