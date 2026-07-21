"""Deterministic, atomic CSV and JSON reports for recruiter-friendly demos."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from .security import redact_text


REVIEW_FIELDS = (
    "role_key",
    "company",
    "title",
    "url",
    "location",
    "score",
    "band",
    "review_state",
)

JOB_FIELDS = (
    "role_key",
    "employer_key",
    "source",
    "external_id",
    "company",
    "title",
    "url",
    "location",
    "employment_type",
    "remote",
    "workplace_type",
    "score",
    "band",
    "reasons",
    "review_state",
    "discovered_at",
    "updated_at",
    "last_seen_at",
    "active",
    "missing_complete_scans",
    "delivered_at",
    "delivery_manifest_id",
)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(_sanitize(value), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: _csv_safe(_sanitize(row.get(field, ""))) for field in fields})
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def export_reports(
    output_dir: Path,
    *,
    jobs: Sequence[Mapping[str, Any]],
    review_queue: Sequence[Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = output_dir / "discovered_roles.json"
    review_path = output_dir / "human_review_queue.csv"
    failures_path = output_dir / "source_failures.json"
    summary_path = output_dir / "scan_summary.json"
    public_jobs = [
        {field: row[field] for field in JOB_FIELDS if field in row}
        for row in jobs
    ]
    atomic_write_json(jobs_path, sorted(public_jobs, key=lambda row: str(row.get("role_key", ""))))
    atomic_write_csv(
        review_path,
        sorted(review_queue, key=lambda row: (-int(row.get("score", 0)), str(row.get("role_key", "")))),
        REVIEW_FIELDS,
    )
    atomic_write_json(failures_path, sorted(failures, key=lambda row: str(row.get("employer_key", ""))))
    atomic_write_json(summary_path, dict(summary))
    return {
        "jobs": str(jobs_path),
        "review_queue": str(review_path),
        "failures": str(failures_path),
        "summary": str(summary_path),
    }


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _csv_safe(value: Any) -> Any:
    """Prevent spreadsheet software from evaluating untrusted public text."""

    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value
