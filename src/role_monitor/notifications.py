"""Provider-neutral, bounded notifications for observable pipeline events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Mapping, Protocol

from .models import compact_text


MAX_NOTIFICATION_TEXT = 4096


@dataclass(frozen=True, slots=True)
class Notification:
    """A plain-text event that is safe to hand to an optional notifier."""

    event: str
    text: str

    def __post_init__(self) -> None:
        event = compact_text(self.event)
        text = str(self.text or "").strip()
        if not event:
            raise ValueError("Notification event is required")
        if not text:
            raise ValueError("Notification text is required")
        if len(text) > MAX_NOTIFICATION_TEXT:
            raise ValueError(f"Notification text cannot exceed {MAX_NOTIFICATION_TEXT} characters")
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "text", text)


@dataclass(frozen=True, slots=True)
class NotificationReceipt:
    """Secret-free evidence returned by a notifier implementation."""

    provider: str
    status: str
    attempts: int
    message_id: int | None = None
    preview: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "status": self.status,
            "attempts": self.attempts,
        }
        if self.message_id is not None:
            result["message_id"] = self.message_id
        if self.preview is not None:
            result["preview"] = self.preview
        return result


class Notifier(Protocol):
    def send(self, notification: Notification) -> NotificationReceipt:
        """Send or preview one notification without changing pipeline state."""


class DryRunNotifier:
    """Return the rendered message without accessing credentials or a network."""

    def __init__(self, provider: str) -> None:
        self.provider = compact_text(provider)
        if not self.provider:
            raise ValueError("Dry-run provider is required")

    def send(self, notification: Notification) -> NotificationReceipt:
        return NotificationReceipt(
            provider=self.provider,
            status="dry-run",
            attempts=0,
            preview=notification.text,
        )


def build_scan_notification(summary: Mapping[str, Any]) -> Notification:
    """Render a deterministic, summary-only scan digest.

    Only explicitly allowlisted counts and the generated timestamp enter the
    message. Reviewer notes, job descriptions, paths, credentials, and
    notification destinations are never accepted by this template.
    """

    values = {
        "generated_at": _timestamp(summary.get("generated_at")),
        "sources_attempted": _count(summary, "sources_attempted"),
        "sources_succeeded": _count(summary, "sources_succeeded"),
        "sources_failed": _count(summary, "sources_failed"),
        "postings_discovered": _count(summary, "postings_discovered"),
        "new_role_records": _count(summary, "new_role_records"),
        "review_queue": _count(summary, "review_queue"),
        "approved_undelivered": _count(summary, "approved_undelivered"),
    }
    digest = sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    text = "\n".join((
        "AI role monitor scan complete",
        f"Scan ID: scan-{digest}",
        f"Generated: {values['generated_at']}",
        (
            "Sources: "
            f"{values['sources_succeeded']}/{values['sources_attempted']} succeeded; "
            f"{values['sources_failed']} failed"
        ),
        (
            f"Postings discovered: {values['postings_discovered']} "
            f"({values['new_role_records']} new)"
        ),
        f"Human review queue: {values['review_queue']}",
        f"Approved, not handed off: {values['approved_undelivered']}",
        "No applications were submitted. Human review remains required.",
    ))
    return Notification(event="scan.completed", text=text)


def _count(summary: Mapping[str, Any], key: str) -> int:
    value = summary.get(key, 0)
    if isinstance(value, bool):
        raise ValueError(f"Scan summary field {key} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Scan summary field {key} must be a non-negative integer") from None
    if number < 0:
        raise ValueError(f"Scan summary field {key} must be a non-negative integer")
    return number


def _timestamp(value: object) -> str:
    rendered = compact_text(value)
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Scan summary generated_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("Scan summary generated_at must include a timezone")
    return parsed.isoformat(timespec="seconds")
