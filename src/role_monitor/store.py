"""Durable, local state for the review-first role-monitor pipeline.

This module intentionally keeps state in SQLite and exposes small, explicit
operations.  Discovery may be repeated freely; a later scan never resets a
person's decision or a verified delivery marker.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .models import ScoredPosting, canonical_url, compact_text


REVIEW_STATES = frozenset({"pending", "approved", "rejected", "deferred", "excluded"})
HANDOFF_ROLE_FIELDS = (
    "role_key",
    "approval_id",
    "company",
    "title",
    "url",
    "location",
    "score",
    "band",
)
MISSING_SCANS_BEFORE_RETIREMENT = 2
HANDOFF_FRESHNESS_SECONDS = 7 * 24 * 60 * 60


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _freshness_cutoff() -> str:
    return (datetime.now(UTC) - timedelta(seconds=HANDOFF_FRESHNESS_SECONDS)).isoformat(timespec="seconds")


def _timestamp_is_fresh(value: object) -> bool:
    try:
        timestamp = datetime.fromisoformat(compact_text(value))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return False
    return timestamp.astimezone(UTC) >= datetime.now(UTC) - timedelta(seconds=HANDOFF_FRESHNESS_SECONDS)


def _role_is_available(row: Mapping[str, Any]) -> bool:
    """Return whether a role is fresh enough for review or handoff."""

    return (
        bool(row["active"])
        and int(row["missing_complete_scans"]) == 0
        and _timestamp_is_fresh(row["last_seen_at"])
    )


class RoleStore:
    """SQLite-backed discoveries, review decisions, and append-only audit data."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database)
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self.initialize()
            self._secure_database_files()
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        self._secure_database_files()
        self._connection.close()
        self._secure_database_files()

    def _secure_database_files(self) -> None:
        if self.database == ":memory:":
            return
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.database}{suffix}")
            if path.exists():
                os.chmod(path, 0o600)

    def initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS roles (
                role_key TEXT PRIMARY KEY,
                employer_key TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                company TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                location TEXT NOT NULL,
                description TEXT NOT NULL,
                employment_type TEXT NOT NULL,
                remote INTEGER,
                workplace_type TEXT NOT NULL DEFAULT 'unknown',
                score INTEGER NOT NULL,
                band TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                missing_complete_scans INTEGER NOT NULL DEFAULT 0,
                review_state TEXT NOT NULL CHECK (review_state IN ('pending', 'approved', 'rejected', 'deferred', 'excluded')),
                review_note TEXT NOT NULL DEFAULT '',
                review_decided_at TEXT,
                delivered_at TEXT,
                delivery_manifest_id TEXT
            );
            CREATE INDEX IF NOT EXISTS roles_url_lookup ON roles(url);
            CREATE INDEX IF NOT EXISTS roles_pending_queue
                ON roles(review_state, delivered_at, score DESC, role_key ASC);
            CREATE TABLE IF NOT EXISTS approval_snapshots (
                role_key TEXT PRIMARY KEY REFERENCES roles(role_key),
                payload_json TEXT NOT NULL,
                approved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS handoff_manifests (
                manifest_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'completed', 'invalidated')),
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                role_key TEXT NOT NULL REFERENCES roles(role_key),
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        self._ensure_role_columns()
        self._connection.commit()
        self._ensure_manifest_schema()
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS handoff_manifests_status "
            "ON handoff_manifests(status, created_at DESC)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS roles_employer_active "
            "ON roles(employer_key, active, role_key)"
        )
        self._backfill_approval_snapshots()
        self._connection.commit()

    def _ensure_role_columns(self) -> None:
        """Add v1 freshness columns to databases created by early local builds."""

        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(roles)").fetchall()
        }
        additions = {
            "employer_key": "TEXT NOT NULL DEFAULT ''",
            "last_seen_at": "TEXT NOT NULL DEFAULT ''",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "workplace_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "missing_complete_scans": "INTEGER NOT NULL DEFAULT 0",
        }
        employer_key_added = "employer_key" not in columns
        for name, definition in additions.items():
            if name not in columns:
                self._connection.execute(f"ALTER TABLE roles ADD COLUMN {name} {definition}")
        self._connection.execute(
            "UPDATE roles SET last_seen_at=updated_at WHERE last_seen_at=''"
        )
        if employer_key_added:
            self._connection.execute(
                "UPDATE roles SET active=0, "
                "review_note='Re-scan required: legacy role had no employer identity.' "
                "WHERE employer_key='' AND delivered_at IS NULL"
            )

    @staticmethod
    def _approval_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
        """Freeze the public fields and evidence that a person actually reviewed."""

        values = dict(row)
        reasons = values.get("reasons", ())
        if "reasons_json" in values:
            reasons = json.loads(str(values["reasons_json"]))
        return {
            "role_key": str(values["role_key"]),
            "employer_key": str(values["employer_key"]),
            "source": str(values["source"]),
            "external_id": str(values["external_id"]),
            "company": str(values["company"]),
            "title": str(values["title"]),
            "url": canonical_url(str(values["url"])),
            "location": str(values["location"]),
            "description": str(values["description"]),
            "employment_type": str(values["employment_type"]),
            "remote": None if values["remote"] is None else bool(values["remote"]),
            "workplace_type": str(values["workplace_type"]),
            "score": int(values["score"]),
            "band": str(values["band"]),
            "reasons": list(reasons),
        }

    def _backfill_approval_snapshots(self) -> None:
        """Require re-review when an older approval has no provable snapshot."""

        rows = self._connection.execute(
            "SELECT roles.*, approval_snapshots.payload_json AS existing_snapshot_json "
            "FROM roles LEFT JOIN approval_snapshots USING(role_key) "
            "WHERE roles.review_state='approved'"
        ).fetchall()
        for row in rows:
            if row["delivered_at"] is not None:
                continue
            try:
                existing = json.loads(row["existing_snapshot_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                existing = {}
            if compact_text(existing.get("approval_id")):
                continue
            self._connection.execute(
                "UPDATE roles SET review_state='pending', "
                "review_note='Re-review required: legacy approval had no immutable snapshot.', "
                "review_decided_at=NULL WHERE role_key=?",
                (row["role_key"],),
            )
            self._connection.execute(
                "DELETE FROM approval_snapshots WHERE role_key=?", (row["role_key"],)
            )
            self._audit(self._connection, row["role_key"], "approval_requeued", {
                "reason": "missing_immutable_snapshot",
            })

    def _ensure_manifest_schema(self) -> None:
        """Upgrade pre-claim v1 databases without discarding pending handoffs."""

        schema_row = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='handoff_manifests'"
        ).fetchone()
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(handoff_manifests)").fetchall()
        }
        schema_sql = str(schema_row["sql"] if schema_row else "").casefold()
        if "claimed_at" in columns and "delivering" in schema_sql:
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("DROP INDEX IF EXISTS handoff_manifests_status")
            self._connection.execute(
                "ALTER TABLE handoff_manifests RENAME TO handoff_manifests_legacy"
            )
            self._connection.execute(
                """
                CREATE TABLE handoff_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'delivering', 'completed', 'invalidated')),
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    completed_at TEXT
                )
                """
            )
            claimed_expression = (
                "claimed_at"
                if "claimed_at" in columns
                else "CASE WHEN status='delivering' THEN ? ELSE NULL END"
            )
            copy_sql = (
                "INSERT INTO handoff_manifests "
                "(manifest_id, payload_json, status, created_at, claimed_at, completed_at) "
                "SELECT manifest_id, payload_json, "
                "CASE WHEN status IN ('pending','delivering','completed','invalidated') "
                "THEN status ELSE 'pending' END, created_at, "
                f"{claimed_expression}, completed_at FROM handoff_manifests_legacy"
            )
            if "claimed_at" in columns:
                self._connection.execute(copy_sql)
            else:
                self._connection.execute(copy_sql, (_now(),))
            self._connection.execute("DROP TABLE handoff_manifests_legacy")
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["reasons"] = tuple(json.loads(result.pop("reasons_json")))
        result["remote"] = None if result["remote"] is None else bool(result["remote"])
        result["active"] = bool(result["active"])
        result["missing_complete_scans"] = int(result["missing_complete_scans"])
        return result

    def _audit(self, connection: sqlite3.Connection, role_key: str, event_type: str, payload: Mapping[str, Any]) -> None:
        connection.execute(
            "INSERT INTO audit_events(role_key, event_type, occurred_at, payload_json) VALUES (?, ?, ?, ?)",
            (role_key, event_type, _now(), json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))),
        )

    def upsert_scored(
        self,
        scored: ScoredPosting,
        *,
        reviewable: bool = True,
        employer_key: str = "",
    ) -> dict[str, Any]:
        """Store the latest public discovery while preserving review/delivery state."""

        if not isinstance(scored.score, int):
            raise ValueError("score must be an integer")
        if not compact_text(scored.band):
            raise ValueError("band is required")
        posting = scored.posting
        role_key = posting.role_key
        now = _now()
        incoming_state = "pending" if reviewable else "excluded"
        values = (
            role_key, compact_text(employer_key or posting.employer_key), posting.source, posting.external_id, posting.company, posting.title,
            canonical_url(posting.url), posting.location, posting.description,
            posting.employment_type, None if posting.remote is None else int(posting.remote), posting.workplace_type,
            scored.score, scored.band, json.dumps(list(scored.reasons)), now, now, now, 1, 0, incoming_state,
        )
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO roles (
                    role_key, employer_key, source, external_id, company, title, url, location, description,
                    employment_type, remote, workplace_type, score, band, reasons_json, discovered_at, updated_at,
                    last_seen_at, active, missing_complete_scans, review_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(role_key) DO UPDATE SET
                    employer_key=CASE WHEN excluded.employer_key<>'' THEN excluded.employer_key ELSE roles.employer_key END,
                    source=excluded.source, external_id=excluded.external_id, company=excluded.company,
                    title=excluded.title, url=excluded.url, location=excluded.location,
                    description=excluded.description, employment_type=excluded.employment_type,
                    remote=excluded.remote, workplace_type=excluded.workplace_type,
                    score=excluded.score, band=excluded.band,
                    reasons_json=excluded.reasons_json, updated_at=excluded.updated_at,
                    last_seen_at=excluded.last_seen_at, active=1, missing_complete_scans=0,
                    review_state=CASE
                        WHEN roles.delivered_at IS NOT NULL THEN roles.review_state
                        WHEN roles.review_state IN ('approved', 'rejected', 'deferred') THEN roles.review_state
                        WHEN excluded.review_state = 'excluded' THEN 'excluded'
                        WHEN roles.review_state = 'excluded' THEN 'pending'
                        ELSE roles.review_state
                    END
                """,
                values,
            )
            self._audit(connection, role_key, "discovery_upserted", {
                "score": scored.score,
                "band": scored.band,
                "reviewable": reviewable,
            })
        return self.get(role_key)

    def retire_missing(self, employer_key: str, seen_role_keys: set[str]) -> int:
        """Pause on one complete miss and retire after consecutive complete misses."""

        employer_key = compact_text(employer_key)
        if not employer_key:
            raise ValueError("employer_key is required to retire missing roles")
        seen = {compact_text(key) for key in seen_role_keys if compact_text(key)}
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT role_key, review_state, delivered_at, missing_complete_scans FROM roles "
                "WHERE employer_key=? AND active=1",
                (employer_key,),
            ).fetchall()
            missing = [row for row in rows if row["role_key"] not in seen]
            if not missing:
                return 0
            manifests = connection.execute(
                "SELECT manifest_id, payload_json, status FROM handoff_manifests "
                "WHERE status IN ('pending', 'delivering')"
            ).fetchall()
            manifest_roles = {
                row["manifest_id"]: {
                    compact_text(item.get("role_key"))
                    for item in json.loads(row["payload_json"]).get("roles", [])
                    if isinstance(item, Mapping)
                }
                for row in manifests
            }
            now = _now()
            retired_count = 0
            for row in missing:
                role_key = str(row["role_key"])
                missing_count = int(row["missing_complete_scans"]) + 1
                in_delivery = any(
                    manifest["status"] == "delivering"
                    and role_key in manifest_roles[manifest["manifest_id"]]
                    for manifest in manifests
                )
                for manifest in manifests:
                    if (
                        manifest["status"] == "pending"
                        and role_key in manifest_roles[manifest["manifest_id"]]
                    ):
                        connection.execute(
                            "UPDATE handoff_manifests SET status='invalidated', claimed_at=NULL "
                            "WHERE manifest_id=? AND status='pending'",
                            (manifest["manifest_id"],),
                        )
                if missing_count < MISSING_SCANS_BEFORE_RETIREMENT:
                    connection.execute(
                        "UPDATE roles SET missing_complete_scans=? WHERE role_key=?",
                        (missing_count, role_key),
                    )
                    self._audit(connection, role_key, "role_missing_observed", {
                        "employer_key": employer_key,
                        "complete_scan_count": missing_count,
                    })
                    continue
                if (
                    row["review_state"] == "approved"
                    and row["delivered_at"] is None
                    and not in_delivery
                ):
                    connection.execute(
                        "UPDATE roles SET active=0, missing_complete_scans=?, updated_at=?, review_state='pending', "
                        "review_note='Re-review required: listing disappeared from a complete source scan.', "
                        "review_decided_at=NULL WHERE role_key=?",
                        (missing_count, now, role_key),
                    )
                else:
                    connection.execute(
                        "UPDATE roles SET active=0, missing_complete_scans=?, updated_at=? WHERE role_key=?",
                        (missing_count, now, role_key),
                    )
                self._audit(connection, role_key, "role_retired", {
                    "employer_key": employer_key,
                    "reason": "absent_from_complete_source_scan",
                })
                retired_count += 1
            return retired_count

    def get(self, role_key: str) -> dict[str, Any]:
        row = self._connection.execute("SELECT * FROM roles WHERE role_key = ?", (role_key,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown role key: {role_key}")
        return self._row_dict(row)

    def contains(self, role_key: str) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM roles WHERE role_key=?", (role_key,)
        ).fetchone() is not None

    def all_roles(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM roles ORDER BY score DESC, role_key ASC"
        ).fetchall()
        return [self._row_dict(row) for row in rows]

    def state_counts(self) -> dict[str, int]:
        counts = {state: 0 for state in REVIEW_STATES}
        cutoff = _freshness_cutoff()
        for row in self._connection.execute(
            "SELECT review_state, COUNT(*) AS count FROM roles "
            "WHERE active=1 AND missing_complete_scans=0 AND last_seen_at>=? "
            "GROUP BY review_state",
            (cutoff,),
        ).fetchall():
            counts[str(row["review_state"])] = int(row["count"])
        counts["delivered"] = int(self._connection.execute(
            "SELECT COUNT(*) FROM roles WHERE delivered_at IS NOT NULL"
        ).fetchone()[0])
        counts["total"] = int(self._connection.execute("SELECT COUNT(*) FROM roles").fetchone()[0])
        counts["active"] = int(self._connection.execute(
            "SELECT COUNT(*) FROM roles WHERE active=1"
        ).fetchone()[0])
        counts["inactive"] = counts["total"] - counts["active"]
        counts["unavailable"] = int(self._connection.execute(
            "SELECT COUNT(*) FROM roles WHERE active=1 "
            "AND (missing_complete_scans>0 OR last_seen_at<?)",
            (cutoff,),
        ).fetchone()[0])
        return counts

    def pending_review(self) -> list[dict[str, Any]]:
        """Return the deterministic queue: strongest score first, then stable identity."""

        rows = self._connection.execute(
            "SELECT * FROM roles WHERE review_state = 'pending' AND active=1 "
            "AND missing_complete_scans=0 AND last_seen_at>=? "
            "AND delivered_at IS NULL ORDER BY score DESC, role_key ASC",
            (_freshness_cutoff(),),
        ).fetchall()
        return [self._row_dict(row) for row in rows]

    def deferred_review(self) -> list[dict[str, Any]]:
        """Return deferred items separately so they are not mistaken for actionable work."""

        rows = self._connection.execute(
            "SELECT * FROM roles WHERE review_state = 'deferred' AND active=1 "
            "AND missing_complete_scans=0 AND last_seen_at>=? "
            "AND delivered_at IS NULL ORDER BY score DESC, role_key ASC",
            (_freshness_cutoff(),),
        ).fetchall()
        return [self._row_dict(row) for row in rows]

    def decide(self, role_key: str, decision: str, note: str = "") -> dict[str, Any]:
        """Make one explicit human decision, with safe state transitions and audit data."""

        decision = compact_text(decision).lower()
        if decision not in {"approved", "rejected", "deferred"}:
            raise ValueError("decision must be approved, rejected, or deferred")
        note = compact_text(note)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM roles WHERE role_key = ?", (role_key,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown role key: {role_key}")
            if row["delivered_at"] is not None:
                raise ValueError("A delivered role cannot be changed")
            if not row["active"]:
                raise ValueError("An inactive role cannot be reviewed until it reappears")
            if row["missing_complete_scans"] or not _timestamp_is_fresh(row["last_seen_at"]):
                raise ValueError("An unavailable or stale role must be re-scanned before review")
            if row["review_state"] == "excluded":
                raise ValueError("An excluded role is not in the review queue")
            if row["review_state"] in {"approved", "rejected"}:
                raise ValueError(f"Cannot change a final {row['review_state']} decision")
            connection.execute(
                "UPDATE roles SET review_state=?, review_note=?, review_decided_at=? WHERE role_key=?",
                (decision, note, _now(), role_key),
            )
            if decision == "approved":
                snapshot = self._approval_snapshot(dict(row))
                snapshot["approval_id"] = "approval-" + uuid4().hex
                connection.execute(
                    "INSERT OR REPLACE INTO approval_snapshots(role_key, payload_json, approved_at) "
                    "VALUES (?, ?, ?)",
                    (
                        role_key,
                        json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                        _now(),
                    ),
                )
            self._audit(connection, role_key, "review_decided", {"decision": decision, "note": note})
        return self.get(role_key)

    def approved_undelivered(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT roles.*, approval_snapshots.payload_json AS approval_payload_json "
            "FROM roles JOIN approval_snapshots USING(role_key) "
            "WHERE roles.review_state='approved' AND roles.delivered_at IS NULL "
            "AND roles.active=1 AND roles.missing_complete_scans=0 "
            "AND roles.last_seen_at>=? "
            "ORDER BY roles.role_key ASC",
            (_freshness_cutoff(),),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            current = self._row_dict(row)
            snapshot = json.loads(current.pop("approval_payload_json"))
            current.update(snapshot)
            results.append(current)
        return results

    def register_manifest(self, manifest: Mapping[str, Any]) -> None:
        """Register a new pending manifest and invalidate any older pending one."""

        manifest_id = compact_text(manifest.get("manifest_id"))
        roles = manifest.get("roles")
        if not manifest_id or not isinstance(roles, list) or not roles:
            raise ValueError("A manifest id and roles are required")
        payload = json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"))
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT status, payload_json FROM handoff_manifests WHERE manifest_id=?", (manifest_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload:
                    raise ValueError("Manifest id is already registered with different content")
                if existing["status"] in {"delivering", "completed"}:
                    # An in-flight manifest owns its durable claim, and a
                    # completed manifest remains a valid idempotent replay target.
                    return
            role_keys = [compact_text(role.get("role_key")) for role in roles if isinstance(role, Mapping)]
            if len(role_keys) != len(roles) or len(set(role_keys)) != len(role_keys):
                raise ValueError("Manifest roles must be unique objects")
            placeholders = ",".join("?" for _ in role_keys)
            approved_rows = connection.execute(
                "SELECT roles.role_key, approval_snapshots.payload_json "
                "FROM roles JOIN approval_snapshots USING(role_key) "
                f"WHERE roles.role_key IN ({placeholders}) AND roles.active=1 "
                "AND roles.missing_complete_scans=0 AND roles.last_seen_at>=? "
                "AND roles.review_state='approved' AND roles.delivered_at IS NULL",
                (*role_keys, _freshness_cutoff()),
            ).fetchall()
            expected_by_key = {
                row["role_key"]: json.loads(row["payload_json"])
                for row in approved_rows
            }
            for role in roles:
                key = compact_text(role["role_key"])
                snapshot = expected_by_key.get(key)
                if snapshot is None:
                    raise ValueError(
                        "Manifest contains a role that is unavailable, stale, or not approved"
                    )
                expected = {field: snapshot.get(field) for field in HANDOFF_ROLE_FIELDS}
                received = {field: role.get(field) for field in HANDOFF_ROLE_FIELDS}
                if received != expected:
                    raise ValueError("Manifest role does not match its immutable approval snapshot")
            if existing is not None and existing["status"] == "pending":
                return
            active_delivery = connection.execute(
                "SELECT manifest_id FROM handoff_manifests WHERE status='delivering' LIMIT 1"
            ).fetchone()
            if active_delivery is not None:
                raise ValueError("Cannot register a new manifest while delivery is in progress")
            connection.execute(
                "UPDATE handoff_manifests SET status='invalidated', claimed_at=NULL "
                "WHERE status='pending' AND manifest_id<>?",
                (manifest_id,),
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO handoff_manifests(manifest_id, payload_json, status, created_at) "
                    "VALUES (?, ?, 'pending', ?)",
                    (manifest_id, payload, _now()),
                )
            else:
                connection.execute(
                    "UPDATE handoff_manifests SET status='pending', claimed_at=NULL, "
                    "completed_at=NULL, created_at=? WHERE manifest_id=? AND status='invalidated'",
                    (_now(), manifest_id),
                )

    def manifest_status(self, manifest: Mapping[str, Any]) -> str:
        """Return registered lifecycle state only when the full payload matches."""

        manifest_id = compact_text(manifest.get("manifest_id"))
        payload = json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"))
        row = self._connection.execute(
            "SELECT payload_json, status FROM handoff_manifests WHERE manifest_id=?", (manifest_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Manifest is not registered")
        if row["payload_json"] != payload:
            raise ValueError("Registered manifest content does not match")
        return str(row["status"])

    @staticmethod
    def _manifest_role_rows(
        connection: sqlite3.Connection,
        manifest: Mapping[str, Any],
    ) -> list[sqlite3.Row]:
        role_keys = [
            compact_text(role.get("role_key"))
            for role in manifest.get("roles", [])
            if isinstance(role, Mapping)
        ]
        if not role_keys or len(role_keys) != len(set(role_keys)):
            return []
        placeholders = ",".join("?" for _ in role_keys)
        return connection.execute(
            f"SELECT role_key, active, missing_complete_scans, last_seen_at, "
            f"review_state, delivered_at FROM roles "
            f"WHERE role_key IN ({placeholders})",
            role_keys,
        ).fetchall()

    def _invalidate_if_unavailable(
        self,
        connection: sqlite3.Connection,
        manifest_id: str,
        manifest: Mapping[str, Any],
    ) -> bool:
        rows = self._manifest_role_rows(connection, manifest)
        expected_count = len(manifest.get("roles", []))
        unavailable = [row for row in rows if not _role_is_available(row)]
        if len(rows) == expected_count and not unavailable:
            return False
        connection.execute(
            "UPDATE handoff_manifests SET status='invalidated', claimed_at=NULL "
            "WHERE manifest_id=?",
            (manifest_id,),
        )
        for row in unavailable:
            if (
                not row["active"]
                and row["review_state"] == "approved"
                and row["delivered_at"] is None
            ):
                connection.execute(
                    "UPDATE roles SET review_state='pending', "
                    "review_note='Re-review required: role became inactive during handoff.', "
                    "review_decided_at=NULL WHERE role_key=?",
                    (row["role_key"],),
                )
            self._audit(connection, row["role_key"], "handoff_invalidated", {
                "manifest_id": manifest_id,
                "reason": "unavailable_role",
            })
        return True

    def claim_manifest(self, manifest: Mapping[str, Any]) -> str:
        """Atomically claim one pending manifest before calling an external adapter."""

        manifest_id = compact_text(manifest.get("manifest_id"))
        payload = json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"))
        invalid_roles = False
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json, status, claimed_at FROM handoff_manifests WHERE manifest_id=?",
                (manifest_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Manifest is not registered")
            if row["payload_json"] != payload:
                raise ValueError("Registered manifest content does not match")
            status = str(row["status"])
            if status == "completed":
                return status
            if status == "invalidated":
                raise ValueError("Cannot deliver a stale manifest")
            if status == "delivering":
                raise ValueError("Manifest delivery is already in progress")
            role_rows = self._manifest_role_rows(connection, manifest)
            expected_count = len(manifest.get("roles", []))
            invalid_roles = (
                len(role_rows) != expected_count
                or any(
                    not _role_is_available(role)
                    or role["review_state"] != "approved"
                    or role["delivered_at"] is not None
                    for role in role_rows
                )
            )
            if invalid_roles:
                connection.execute(
                    "UPDATE handoff_manifests SET status='invalidated', claimed_at=NULL "
                    "WHERE manifest_id=?",
                    (manifest_id,),
                )
            else:
                now = _now()
                changed = connection.execute(
                    "UPDATE handoff_manifests SET status='delivering', claimed_at=? "
                    "WHERE manifest_id=? AND status='pending'",
                    (now, manifest_id),
                ).rowcount
                if changed != 1:
                    raise ValueError("Manifest could not be claimed for delivery")
        if invalid_roles:
            raise ValueError(
                "Manifest contains a role that is unavailable, stale, or no longer approved"
            )
        return "delivering"

    def release_manifest(self, manifest: Mapping[str, Any]) -> None:
        """Release a local claim after an adapter or receipt failure."""

        manifest_id = compact_text(manifest.get("manifest_id"))
        payload = json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json, status FROM handoff_manifests WHERE manifest_id=?", (manifest_id,)
            ).fetchone()
            if row is None or row["payload_json"] != payload:
                raise ValueError("Registered manifest content does not match")
            if row["status"] == "delivering":
                if not self._invalidate_if_unavailable(connection, manifest_id, manifest):
                    connection.execute(
                        "UPDATE handoff_manifests SET status='pending', claimed_at=NULL WHERE manifest_id=?",
                        (manifest_id,),
                    )

    def recover_manifest(self, manifest: Mapping[str, Any]) -> str:
        """Requeue a stranded claim after an operator reconciles downstream state."""

        manifest_id = compact_text(manifest.get("manifest_id"))
        payload = json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json, status FROM handoff_manifests WHERE manifest_id=?", (manifest_id,)
            ).fetchone()
            if row is None or row["payload_json"] != payload:
                raise ValueError("Registered manifest content does not match")
            if row["status"] != "delivering":
                raise ValueError("Only an in-progress manifest can be recovered")
            if self._invalidate_if_unavailable(connection, manifest_id, manifest):
                return_status = "invalidated"
            else:
                connection.execute(
                    "UPDATE handoff_manifests SET status='pending', claimed_at=NULL WHERE manifest_id=?",
                    (manifest_id,),
                )
                return_status = "pending"
            for role in manifest.get("roles", []):
                role_key = compact_text(role.get("role_key")) if isinstance(role, Mapping) else ""
                if role_key:
                    self._audit(
                        connection,
                        role_key,
                        "handoff_recovery_confirmed",
                        {"manifest_id": manifest_id},
                    )
        return return_status

    def mark_delivered(self, manifest_id: str, roles: list[Mapping[str, str]]) -> None:
        """Mark exactly the still-approved roles delivered after receipt verification."""

        manifest_id = compact_text(manifest_id)
        if not manifest_id:
            raise ValueError("manifest_id is required")
        role_keys = [compact_text(role.get("role_key")) for role in roles]
        if not role_keys or len(set(role_keys)) != len(role_keys):
            raise ValueError("roles must have unique role keys")
        with self._transaction() as connection:
            manifest = connection.execute(
                "SELECT payload_json, status FROM handoff_manifests WHERE manifest_id=?", (manifest_id,)
            ).fetchone()
            if manifest is None:
                raise ValueError("Receipt references an unregistered manifest")
            registered_roles = json.loads(manifest["payload_json"]).get("roles", [])
            expected = {(compact_text(item.get("role_key")), canonical_url(item.get("url", ""))) for item in registered_roles}
            supplied = {(compact_text(item.get("role_key")), canonical_url(item.get("url", ""))) for item in roles}
            if len(supplied) != len(roles) or supplied != expected:
                raise ValueError("Receipt roles do not exactly match registered manifest roles")
            if manifest["status"] == "invalidated":
                raise ValueError("Receipt references a stale manifest")
            rows = connection.execute(
                f"SELECT role_key, url, active, missing_complete_scans, last_seen_at, "
                f"review_state, delivered_at, delivery_manifest_id FROM roles "
                f"WHERE role_key IN ({','.join('?' for _ in role_keys)})",
                role_keys,
            ).fetchall()
            if len(rows) != len(role_keys):
                raise ValueError("Receipt references an unknown role")
            by_key = {row["role_key"]: row for row in rows}
            if manifest["status"] == "completed":
                if all(row["delivered_at"] is not None and row["delivery_manifest_id"] == manifest_id for row in rows):
                    return
                raise ValueError("Completed manifest does not match delivery state")
            if manifest["status"] != "delivering" and any(
                not _role_is_available(row) for row in rows
            ):
                raise ValueError("A pending manifest contains an unavailable or stale role")
            for role in roles:
                key = compact_text(role["role_key"])
                row = by_key[key]
                if row["delivered_at"] is not None and row["delivery_manifest_id"] != manifest_id:
                    raise ValueError("Role has already been delivered by another manifest")
                if row["delivered_at"] is None and row["review_state"] != "approved":
                    raise ValueError("Only approved roles may be delivered")
            now = _now()
            for key in role_keys:
                row = by_key[key]
                if row["delivered_at"] is None:
                    connection.execute(
                        "UPDATE roles SET delivered_at=?, delivery_manifest_id=? WHERE role_key=?",
                        (now, manifest_id, key),
                    )
                    self._audit(connection, key, "handoff_verified", {"manifest_id": manifest_id})
            connection.execute(
                "UPDATE handoff_manifests SET status='completed', claimed_at=NULL, completed_at=? "
                "WHERE manifest_id=? AND status IN ('pending', 'delivering')",
                (now, manifest_id),
            )

    def audit_events(self, role_key: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT event_id, role_key, event_type, occurred_at, payload_json FROM audit_events "
            "WHERE role_key=? ORDER BY event_id ASC", (role_key,)
        ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]


SQLiteRoleStore = RoleStore
