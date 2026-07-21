"""Offline, fixture-backed demonstration of the complete review-first flow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import MonitorConfig, load_config
from .feeds import resolve_feed
from .handoff import InMemoryTaskAdapter, build_manifest, deliver_manifest, write_manifest_atomic
from .http import MappingTransport, json_response
from .pipeline import PipelineRun, run_pipeline
from .reports import atomic_write_json
from .store import RoleStore


SYNTHETIC_DECISION_NOTE = "Synthetic demo decision: approved for in-memory handoff demonstration."


@dataclass(frozen=True, slots=True)
class DemoRun:
    """Artifacts and outcomes from a demonstration that never contacts a network."""

    pipeline: PipelineRun
    approved_role_keys: tuple[str, ...]
    manifest: dict[str, Any]
    receipt: dict[str, Any]
    database_path: str
    manifest_path: str
    receipt_path: str
    task_count: int
    final_state: dict[str, int]


def fixture_transport(config: MonitorConfig, fixtures_dir: Path) -> MappingTransport:
    """Load only local ATS fixture responses for every configured employer."""

    responses = {}
    for employer in config.employers:
        request = resolve_feed(employer.careers_url)
        if request is None:
            raise ValueError(f"Offline demo needs a recognized ATS fixture for {employer.key}")
        fixture_path = fixtures_dir / f"{request.adapter}.json"
        if not fixture_path.is_file():
            raise FileNotFoundError(f"Missing offline fixture: {fixture_path}")
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        responses[request.url] = json_response(request.url, payload)
    return MappingTransport(responses)


def run_demo(
    config_path: Path,
    output_dir: Path,
    *,
    fixtures_dir: Path | None = None,
    reset: bool = False,
) -> DemoRun:
    """Run discovery, a clearly-labelled synthetic approval, and in-memory delivery.

    The demo's only transport is :class:`MappingTransport`; it cannot fetch live
    employers even when the supplied configuration contains public URLs.
    """

    config = load_config(config_path)
    fixture_root = fixtures_dir or config_path.parent / "examples" / "fixtures" / "ats"
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = output_dir / "demo_roles.sqlite3"
    if reset:
        for path in (
            database_path,
            Path(f"{database_path}-shm"),
            Path(f"{database_path}-wal"),
            output_dir / "handoff_manifest.json",
            output_dir / "handoff_receipt.json",
        ):
            path.unlink(missing_ok=True)
    elif database_path.exists():
        raise FileExistsError(
            f"Demo state already exists at {database_path}; pass --reset to replace only the demo artifacts"
        )
    store = RoleStore(database_path)
    try:
        pipeline = run_pipeline(
            config,
            fixture_transport(config, fixture_root),
            store,
            output_dir / "reports",
        )
        queue = store.pending_review()
        if not queue:
            raise ValueError("Offline demo fixtures produced no reviewable roles")
        selected = queue[0]
        store.decide(selected["role_key"], "approved", SYNTHETIC_DECISION_NOTE)
        manifest = build_manifest(store)
        manifest_path = write_manifest_atomic(manifest, output_dir / "handoff_manifest.json")
        adapter = InMemoryTaskAdapter()
        receipt = dict(deliver_manifest(store, manifest, adapter))
        receipt_path = output_dir / "handoff_receipt.json"
        atomic_write_json(receipt_path, receipt)
        final_state = store.state_counts()
        return DemoRun(
            pipeline=pipeline,
            approved_role_keys=(selected["role_key"],),
            manifest=manifest,
            receipt=receipt,
            database_path=str(database_path),
            manifest_path=str(manifest_path),
            receipt_path=str(receipt_path),
            task_count=len(adapter.tasks),
            final_state=final_state,
        )
    finally:
        store.close()
