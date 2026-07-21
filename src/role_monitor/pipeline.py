"""Application orchestration for discovery, scoring, persistence, and reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import MonitorConfig
from .discovery import DiscoveryResult, discover_all
from .http import HttpTransport
from .models import ScoredPosting
from .policy import score_postings
from .reports import export_reports
from .store import RoleStore


@dataclass(frozen=True, slots=True)
class PipelineRun:
    discoveries: tuple[DiscoveryResult, ...]
    scored: tuple[ScoredPosting, ...]
    records: tuple[dict[str, Any], ...]
    failures: tuple[dict[str, Any], ...]
    summary: dict[str, Any]
    reports: dict[str, str]


def run_pipeline(
    config: MonitorConfig,
    transport: HttpTransport,
    store: RoleStore,
    output_dir: Path,
) -> PipelineRun:
    discoveries = discover_all(
        config.employers,
        transport,
        config.retry,
        max_workers=config.concurrency,
        user_agent=config.user_agent,
    )
    postings = [posting for result in discoveries if result.ok for posting in result.jobs]
    employer_by_role_key = {
        posting.role_key: result.employer.key
        for result in discoveries
        if result.ok
        for posting in result.jobs
    }
    scored = score_postings(postings, config.scoring)
    records: list[dict[str, Any]] = []
    new_count = 0
    for item in scored:
        existed = store.contains(item.posting.role_key)
        row = store.upsert_scored(
            item,
            reviewable=config.scoring.reviewable(item),
            employer_key=employer_by_role_key[item.posting.role_key],
        )
        if not existed:
            new_count += 1
        records.append(row)
    retired_count = 0
    for result in discoveries:
        if result.ok and result.complete:
            retired_count += store.retire_missing(
                result.employer.key,
                {posting.role_key for posting in result.jobs},
            )
    failures = tuple(
        result.failure.as_dict()
        for result in discoveries
        if result.failure is not None
    )
    counts = store.state_counts()
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sources_attempted": len(discoveries),
        "sources_succeeded": sum(result.ok for result in discoveries),
        "sources_failed": sum(not result.ok for result in discoveries),
        "complete_native_sources": sum(result.ok and result.complete for result in discoveries),
        "generic_partial_sources": sum(result.ok and not result.complete for result in discoveries),
        "postings_discovered": len(postings),
        "new_role_records": new_count,
        "roles_retired": retired_count,
        "review_queue": counts["pending"],
        "approved_undelivered": len(store.approved_undelivered()),
        "delivered": counts["delivered"],
        "excluded": counts["excluded"],
        "active_role_records": counts["active"],
        "inactive_role_records": counts["inactive"],
        "unavailable_role_records": counts["unavailable"],
    }
    job_rows = [_report_row(row) for row in store.all_roles()]
    review_rows = [_report_row(row) for row in store.pending_review()]
    reports = export_reports(
        output_dir,
        jobs=job_rows,
        review_queue=review_rows,
        failures=list(failures),
        summary=summary,
    )
    return PipelineRun(
        discoveries=tuple(discoveries),
        scored=tuple(scored),
        records=tuple(records),
        failures=failures,
        summary=summary,
        reports=reports,
    )


def _report_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "role_key": row["role_key"],
        "source": row["source"],
        "external_id": row["external_id"],
        "company": row["company"],
        "title": row["title"],
        "url": row["url"],
        "location": row["location"],
        "employment_type": row["employment_type"],
        "remote": row["remote"],
        "workplace_type": row["workplace_type"],
        "score": row["score"],
        "band": row["band"],
        "reasons": list(row["reasons"]),
        "review_state": row["review_state"],
        "discovered_at": row["discovered_at"],
        "updated_at": row["updated_at"],
        "delivered_at": row["delivered_at"],
        "delivery_manifest_id": row["delivery_manifest_id"],
        "employer_key": row["employer_key"],
        "last_seen_at": row["last_seen_at"],
        "active": row["active"],
        "missing_complete_scans": row["missing_complete_scans"],
    }
