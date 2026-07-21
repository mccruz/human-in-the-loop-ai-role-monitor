"""Validated JSON configuration with no embedded authentication fields."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .http import RetryPolicy
from .models import Employer, compact_text
from .policy import ScoringPolicy


FORBIDDEN_CONFIG_TERMS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "signature",
    "token",
}


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    employers: tuple[Employer, ...]
    scoring: ScoringPolicy
    retry: RetryPolicy
    concurrency: int
    database: str
    output_dir: str
    user_agent: str


def load_config(path: Path) -> MonitorConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Configuration must use schema_version 1")
    _reject_auth_fields(payload)
    employer_rows = payload.get("employers")
    if not isinstance(employer_rows, list) or not employer_rows:
        raise ValueError("Configuration must contain at least one employer")
    employers = tuple(
        Employer(
            key=compact_text(row.get("key")),
            name=compact_text(row.get("name")),
            careers_url=compact_text(row.get("careers_url")),
        )
        for row in employer_rows
        if isinstance(row, Mapping)
    )
    if len(employers) != len(employer_rows):
        raise ValueError("Every employer entry must be an object")
    keys = [employer.key.casefold() for employer in employers]
    if len(keys) != len(set(keys)):
        raise ValueError("Employer keys must be unique regardless of letter case")
    retry_values = payload.get("retry", {})
    retry = RetryPolicy(
        attempts=int(retry_values.get("attempts", 3)),
        timeout_seconds=float(retry_values.get("timeout_seconds", 15)),
        backoff_seconds=float(retry_values.get("backoff_seconds", 0.25)),
    )
    concurrency = int(payload.get("concurrency", 6))
    if not 1 <= concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32")
    return MonitorConfig(
        employers=employers,
        scoring=ScoringPolicy.from_mapping(payload.get("scoring", {})),
        retry=retry,
        concurrency=concurrency,
        database=compact_text(payload.get("database") or "output/roles.sqlite3"),
        output_dir=compact_text(payload.get("output_dir") or "output/reports"),
        user_agent=compact_text(payload.get("user_agent") or "human-in-the-loop-ai-role-monitor/1.0"),
    )


def _reject_auth_fields(value: Any, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(term in normalized for term in FORBIDDEN_CONFIG_TERMS):
                raise ValueError(f"Authentication field {path}.{key} is not allowed in repository configuration")
            _reject_auth_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_auth_fields(item, f"{path}[{index}]")
