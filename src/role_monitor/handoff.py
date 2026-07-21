"""Verified, replay-safe handoff of already-approved roles.

Adapters receive a public manifest only.  Credentials are deliberately not a
configuration concern of this module: callers can inject a sender at runtime.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Protocol, Sequence

from .models import canonical_url, compact_text
from .security import safe_error
from .store import HANDOFF_ROLE_FIELDS, RoleStore


MANIFEST_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1


def redact_external_error(error: object) -> str:
    """Keep useful diagnostics while removing common credential-shaped values."""

    return safe_error(error, limit=500)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _manifest_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": manifest.get("schema_version"), "roles": manifest.get("roles")}


def _manifest_id(payload: Mapping[str, Any]) -> str:
    return "manifest-" + sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:24]


def build_manifest(store: RoleStore) -> dict[str, Any]:
    """Build a deterministic v1 manifest from only approved, undelivered roles."""

    roles = [
        {
            "role_key": row["role_key"],
            "approval_id": row["approval_id"],
            "company": row["company"],
            "title": row["title"],
            "url": canonical_url(row["url"]),
            "location": row["location"],
            "score": row["score"],
            "band": row["band"],
        }
        for row in store.approved_undelivered()
    ]
    if not roles:
        raise ValueError("No approved, undelivered roles are available for handoff")
    role_keys = [role["role_key"] for role in roles]
    urls = [role["url"] for role in roles]
    if len(role_keys) != len(set(role_keys)) or len(urls) != len(set(urls)):
        raise ValueError("Manifest roles must have unique role keys and URLs")
    payload = {"schema_version": MANIFEST_SCHEMA_VERSION, "roles": roles}
    manifest = {**payload, "manifest_id": _manifest_id(payload)}
    store.register_manifest(manifest)
    return manifest


def write_manifest_atomic(manifest: Mapping[str, Any], destination: str | Path) -> Path:
    """Atomically replace a JSON manifest; readers never observe partial JSON."""

    validate_manifest(manifest)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(manifest), handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return target


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if set(manifest) != {"schema_version", "roles", "manifest_id"}:
        raise ValueError("Manifest contains unknown or missing top-level fields")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unsupported manifest schema version")
    if not compact_text(manifest.get("manifest_id")):
        raise ValueError("Manifest id is required")
    roles = manifest.get("roles")
    if not isinstance(roles, list) or not roles:
        raise ValueError("Manifest must contain at least one role")
    keys: set[str] = set()
    urls: set[str] = set()
    for role in roles:
        if not isinstance(role, Mapping):
            raise ValueError("Manifest role must be an object")
        if set(role) != set(HANDOFF_ROLE_FIELDS):
            raise ValueError("Manifest role contains unknown or missing fields")
        key = compact_text(role.get("role_key"))
        url = canonical_url(str(role.get("url", "")))
        if not key or key in keys or url in urls:
            raise ValueError("Manifest role keys and URLs must be unique")
        keys.add(key)
        urls.add(url)
    if manifest["manifest_id"] != _manifest_id(_manifest_payload(manifest)):
        raise ValueError("Manifest id does not match manifest content")


class TaskSystemAdapter(Protocol):
    def upsert_manifest(self, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        """Upsert tasks and return a v1 receipt."""


class InMemoryTaskAdapter:
    """Test/demonstration adapter whose existing task state is never reset."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}

    def upsert_manifest(self, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        validate_manifest(manifest)
        for role in manifest["roles"]:
            key = role["role_key"]
            existing = self.tasks.get(key)
            if existing is None:
                self.tasks[key] = {**dict(role), "state": "open"}
            else:
                state = existing["state"]
                existing.update(dict(role))
                existing["state"] = state
        return complete_receipt(manifest)


HttpSender = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class WebhookTaskAdapter:
    """Optional transport adapter with caller-injected sending and no stored secrets."""

    def __init__(self, endpoint: str, sender: HttpSender) -> None:
        self.endpoint = canonical_url(endpoint)
        self._sender = sender

    def upsert_manifest(self, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        validate_manifest(manifest)
        try:
            receipt = self._sender(self.endpoint, dict(manifest))
        except Exception as exc:
            raise RuntimeError(f"Task-system handoff failed: {redact_external_error(exc)}") from None
        if not isinstance(receipt, Mapping):
            raise RuntimeError("Task-system handoff returned a non-object receipt")
        return receipt


def complete_receipt(manifest: Mapping[str, Any]) -> dict[str, Any]:
    validate_manifest(manifest)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "manifest_id": manifest["manifest_id"],
        "status": "complete",
        "roles": [{"role_key": role["role_key"], "url": role["url"]} for role in manifest["roles"]],
    }


def verify_receipt(manifest: Mapping[str, Any], receipt: Mapping[str, Any]) -> list[dict[str, str]]:
    """Require a complete, exact receipt; partial/stale/wrong receipts are rejected."""

    validate_manifest(manifest)
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError("Unsupported receipt schema version")
    if receipt.get("status") != "complete" or receipt.get("manifest_id") != manifest["manifest_id"]:
        raise ValueError("Receipt does not verify this complete manifest")
    receipt_roles = receipt.get("roles")
    if not isinstance(receipt_roles, Sequence) or isinstance(receipt_roles, (str, bytes)):
        raise ValueError("Receipt roles must be a list")
    expected = {(role["role_key"], canonical_url(role["url"])) for role in manifest["roles"]}
    actual: set[tuple[str, str]] = set()
    normalized: list[dict[str, str]] = []
    for role in receipt_roles:
        if not isinstance(role, Mapping):
            raise ValueError("Receipt role must be an object")
        item = (compact_text(role.get("role_key")), canonical_url(str(role.get("url", ""))))
        actual.add(item)
        normalized.append({"role_key": item[0], "url": item[1]})
    if len(normalized) != len(actual) or actual != expected:
        raise ValueError("Receipt roles do not exactly match manifest roles")
    return normalized


def deliver_manifest(store: RoleStore, manifest: Mapping[str, Any], adapter: TaskSystemAdapter) -> Mapping[str, Any]:
    """Deliver once or replay safely; only a verified full receipt updates SQLite."""

    validate_manifest(manifest)
    status = store.claim_manifest(manifest)
    if status == "completed":
        return complete_receipt(manifest)
    try:
        receipt = adapter.upsert_manifest(manifest)
        acknowledge_receipt(store, manifest, receipt)
    except Exception:
        store.release_manifest(manifest)
        raise
    return receipt


def acknowledge_receipt(
    store: RoleStore,
    manifest: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    """Apply an externally produced receipt only after exact verification."""

    roles = verify_receipt(manifest, receipt)
    store.mark_delivered(str(manifest["manifest_id"]), roles)
