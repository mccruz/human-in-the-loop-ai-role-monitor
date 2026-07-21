"""Configurable, explainable scoring for a human review queue."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping

from .models import JobPosting, ScoredPosting, compact_text


@dataclass(frozen=True, slots=True)
class WeightedTerm:
    term: str
    weight: int
    fields: tuple[str, ...] = ("title", "description")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WeightedTerm":
        term = compact_text(value.get("term"))
        fields = tuple(compact_text(item) for item in value.get("fields", ("title", "description")))
        if not term:
            raise ValueError("Policy terms cannot be empty")
        if not fields or any(field not in {"title", "description", "location", "employment_type", "remote", "workplace_type"} for field in fields):
            raise ValueError(f"Unsupported scoring fields for {term!r}")
        return cls(term=term, weight=int(value.get("weight", 0)), fields=fields)


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    terms: tuple[WeightedTerm, ...]
    high_threshold: int = 70
    medium_threshold: int = 45
    review_threshold: int = 30
    minimum_score: int = 0
    maximum_score: int = 100

    def __post_init__(self) -> None:
        if not self.minimum_score <= self.review_threshold <= self.medium_threshold <= self.high_threshold <= self.maximum_score:
            raise ValueError("Policy thresholds must be ordered within the score range")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScoringPolicy":
        terms = tuple(WeightedTerm.from_mapping(item) for item in value.get("terms", ()))
        if not terms:
            raise ValueError("At least one scoring term is required")
        thresholds = value.get("thresholds", {})
        limits = value.get("limits", {})
        return cls(
            terms=terms,
            high_threshold=int(thresholds.get("high", 70)),
            medium_threshold=int(thresholds.get("medium", 45)),
            review_threshold=int(thresholds.get("review", 30)),
            minimum_score=int(limits.get("minimum", 0)),
            maximum_score=int(limits.get("maximum", 100)),
        )

    def score(self, posting: JobPosting) -> ScoredPosting:
        searchable = {
            "title": compact_text(posting.title).casefold(),
            "description": compact_text(posting.description).casefold(),
            "location": compact_text(posting.location).casefold(),
            "employment_type": compact_text(posting.employment_type).casefold(),
            "remote": "remote" if posting.remote is True else ("on-site" if posting.remote is False else "unknown"),
            "workplace_type": posting.workplace_type,
        }
        score = 0
        reasons: list[str] = []
        for rule in self.terms:
            pattern = _term_pattern(rule.term)
            matched_fields = [field for field in rule.fields if pattern.search(searchable[field])]
            if not matched_fields:
                continue
            score += rule.weight
            direction = "+" if rule.weight >= 0 else ""
            reasons.append(f"{direction}{rule.weight} {rule.term} in {', '.join(matched_fields)}")
        bounded = max(self.minimum_score, min(self.maximum_score, score))
        if bounded >= self.high_threshold:
            band = "High"
        elif bounded >= self.medium_threshold:
            band = "Medium"
        elif bounded >= self.review_threshold:
            band = "Review"
        else:
            band = "Below threshold"
        if not reasons:
            reasons.append("No configured terms matched")
        return ScoredPosting(posting=posting, score=bounded, band=band, reasons=tuple(reasons))

    def reviewable(self, scored: ScoredPosting) -> bool:
        return scored.score >= self.review_threshold


def score_postings(postings: Iterable[JobPosting], policy: ScoringPolicy) -> list[ScoredPosting]:
    """Score postings deterministically and return them in stable priority order."""

    scored = [policy.score(posting) for posting in postings]
    return sorted(
        scored,
        key=lambda item: (-item.score, item.posting.company.casefold(), item.posting.title.casefold(), item.posting.role_key),
    )


def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(compact_text(term).casefold()).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![\w-]){escaped}(?![\w-])", re.IGNORECASE)
