from __future__ import annotations

import unittest

from role_monitor.models import JobPosting
from role_monitor.policy import ScoringPolicy, score_postings


def sample_policy() -> ScoringPolicy:
    return ScoringPolicy.from_mapping({
        "thresholds": {"high": 70, "medium": 45, "review": 30},
        "terms": [
            {"term": "AI automation", "weight": 40, "fields": ["title", "description"]},
            {"term": "workflow", "weight": 25, "fields": ["title", "description"]},
            {"term": "implementation", "weight": 20, "fields": ["title", "description"]},
            {"term": "machine learning researcher", "weight": -60, "fields": ["title"]},
            {"term": "remote", "weight": 10, "fields": ["location"]},
        ],
    })


def posting(title: str, description: str = "", location: str = "") -> JobPosting:
    return JobPosting(
        source="synthetic",
        external_id=title,
        company="Example Automation",
        title=title,
        url=f"https://example.test/jobs/{len(title)}",
        description=description,
        location=location,
    )


class ScoringPolicyTests(unittest.TestCase):
    def test_explainable_score_and_band(self):
        result = sample_policy().score(posting(
            "AI Automation Implementation Specialist",
            "Build workflow integrations.",
            "Remote",
        ))
        self.assertEqual(95, result.score)
        self.assertEqual("High", result.band)
        self.assertIn("+40 AI automation in title", result.reasons)

    def test_negative_rule_is_bounded(self):
        result = sample_policy().score(posting("Machine Learning Researcher"))
        self.assertEqual(0, result.score)
        self.assertEqual("Below threshold", result.band)

    def test_matching_uses_term_boundaries(self):
        policy = ScoringPolicy.from_mapping({
            "terms": [{"term": "AI", "weight": 100, "fields": ["title"]}],
        })
        self.assertEqual(0, policy.score(posting("Retail Operations")).score)
        self.assertEqual(100, policy.score(posting("AI Operations")).score)

    def test_invalid_threshold_order_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "thresholds"):
            ScoringPolicy.from_mapping({
                "thresholds": {"review": 80, "medium": 40, "high": 70},
                "terms": [{"term": "workflow", "weight": 10}],
            })

    def test_results_have_stable_priority_order(self):
        results = score_postings([
            posting("Workflow Coordinator"),
            posting("AI Automation Specialist", "workflow implementation"),
        ], sample_policy())
        self.assertEqual("AI Automation Specialist", results[0].posting.title)

    def test_explicit_remote_flag_scores_even_when_city_location_lacks_word(self):
        policy = ScoringPolicy.from_mapping({
            "terms": [{"term": "remote", "weight": 10, "fields": ["remote"]}],
        })
        role = JobPosting(
            "smartrecruiters",
            "1",
            "Example",
            "Automation Engineer",
            "https://example.test/jobs/1",
            location="Manila",
            remote=True,
        )
        self.assertEqual(10, policy.score(role).score)


if __name__ == "__main__":
    unittest.main()
