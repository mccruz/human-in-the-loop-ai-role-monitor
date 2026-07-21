"""Provider-neutral domain models and stable identity helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import ipaddress
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = {
    "gh_src",
    "source",
    "src",
    "ref",
    "referrer",
    "lever-source",
}


def compact_text(value: Any) -> str:
    """Return a single-line representation suitable for logs and reports."""

    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_url(value: str) -> str:
    """Normalize a public job URL while removing common tracking parameters."""

    parsed = urlsplit(compact_text(value))
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError(f"Expected an absolute HTTPS URL, received {value!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credential-bearing URLs are not allowed")
    filtered_query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), host, path, urlencode(sorted(filtered_query)), ""))


def require_public_hostname(hostname: str) -> None:
    """Reject literal/private and conventional local-network destinations."""

    host = compact_text(hostname).casefold().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        raise ValueError("Public source URL cannot target a local hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Public source URL cannot target a non-public IP address")


def stable_role_key(
    source: str,
    external_id: str,
    company: str,
    title: str,
    url: str,
    employer_key: str = "",
) -> str:
    """Build a deterministic, non-personal deduplication key."""

    del title  # Titles are mutable presentation data, not posting identity.
    if compact_text(external_id):
        parts = (source, employer_key or company, external_id)
    else:
        parts = (source, canonical_url(url))
    identity = "\x1f".join(compact_text(part).casefold() for part in parts)
    return "role-" + sha256(identity.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class Employer:
    key: str
    name: str
    careers_url: str

    def __post_init__(self) -> None:
        if not compact_text(self.key) or not compact_text(self.name):
            raise ValueError("Employer key and name are required")
        canonical_url(self.careers_url)
        require_public_hostname(urlsplit(self.careers_url).hostname or "")


@dataclass(frozen=True, slots=True)
class JobPosting:
    source: str
    external_id: str
    company: str
    title: str
    url: str
    location: str = ""
    description: str = ""
    employment_type: str = ""
    remote: bool | None = None
    employer_key: str = ""
    workplace_type: str = "unknown"

    def __post_init__(self) -> None:
        for label, value in (
            ("source", self.source),
            ("company", self.company),
            ("title", self.title),
        ):
            if not compact_text(value):
                raise ValueError(f"Job {label} is required")
        canonical_url(self.url)
        require_public_hostname(urlsplit(self.url).hostname or "")
        if self.workplace_type not in {"remote", "hybrid", "on-site", "unknown"}:
            raise ValueError("workplace_type must be remote, hybrid, on-site, or unknown")

    @property
    def role_key(self) -> str:
        return stable_role_key(
            self.source,
            self.external_id,
            self.company,
            self.title,
            self.url,
            self.employer_key,
        )

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["url"] = canonical_url(self.url)
        result["role_key"] = self.role_key
        return result


@dataclass(frozen=True, slots=True)
class ScoredPosting:
    posting: JobPosting
    score: int
    band: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.posting.as_dict(),
            "score": self.score,
            "band": self.band,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ScanFailure:
    employer_key: str
    employer_name: str
    adapter: str
    error: str
    attempts: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
