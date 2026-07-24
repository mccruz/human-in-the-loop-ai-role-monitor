"""Command-line interface for the review-first role monitor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .config import MonitorConfig, load_config
from .demo import run_demo
from .handoff import acknowledge_receipt, build_manifest, write_manifest_atomic
from .http import UrllibTransport
from .notifications import DryRunNotifier, build_scan_notification
from .pipeline import run_pipeline
from .review import ReviewService
from .store import RoleStore
from .telegram import NotificationError, TelegramNotifier, load_telegram_credentials


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="role-monitor", description="Human-reviewed AI-role monitor")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--config", type=Path, required=True, help="Path to schema-version-1 JSON configuration")
        command.add_argument("--database", type=Path, help="Override the configured SQLite database path")

    scan = commands.add_parser("scan", help="Discover public roles; never makes review decisions")
    common(scan)
    scan.add_argument("--output-dir", type=Path, help="Override the configured report directory")
    scan.add_argument("--allow-network", action="store_true", help="Required before live public-feed requests are made")
    scan_notifications = scan.add_mutually_exclusive_group()
    scan_notifications.add_argument(
        "--notify-telegram",
        action="store_true",
        help="Send one summary-only digest using environment-provided Telegram credentials",
    )
    scan_notifications.add_argument(
        "--telegram-dry-run",
        action="store_true",
        help="Render the Telegram digest without credentials or a Telegram request",
    )

    queue = commands.add_parser("queue", help="Show pending and deferred human-review items")
    common(queue)

    review = commands.add_parser("review", help="Record one explicit human review decision")
    common(review)
    review.add_argument("role_key")
    review.add_argument("decision", choices=("approved", "rejected", "deferred"))
    review.add_argument("--note", default="", help="Optional reviewer rationale")

    handoff = commands.add_parser("prepare-handoff", help="Write a manifest from already-approved roles")
    common(handoff)
    handoff.add_argument("--destination", type=Path, required=True, help="JSON manifest destination")

    receipt = commands.add_parser(
        "acknowledge-receipt",
        help="Verify an exact external receipt before marking roles delivered",
    )
    common(receipt)
    receipt.add_argument("--manifest", type=Path, required=True, help="Previously prepared manifest JSON")
    receipt.add_argument("--receipt", type=Path, required=True, help="Receipt JSON returned by the task system")

    recovery = commands.add_parser(
        "recover-handoff",
        help="Requeue a stranded delivery only after checking the downstream system",
    )
    common(recovery)
    recovery.add_argument("--manifest", type=Path, required=True, help="Stranded manifest JSON")
    recovery.add_argument(
        "--confirm-downstream-reconciled",
        action="store_true",
        help="Confirm downstream state was checked and an idempotent retry is safe",
    )

    demo = commands.add_parser("demo", help="Run the fully offline fixture-backed demonstration")
    demo.add_argument("--config", type=Path, default=Path("config.example.json"))
    demo.add_argument("--output-dir", type=Path, default=Path("output/demo"))
    demo.add_argument("--fixtures-dir", type=Path, help="ATS fixture directory; defaults beside the config")
    demo.add_argument(
        "--reset",
        action="store_true",
        help="Replace only the known artifacts inside the selected demo output directory",
    )
    demo.add_argument(
        "--telegram-dry-run",
        action="store_true",
        help="Render a synthetic Telegram digest without credentials or a network request",
    )
    return parser


def _config_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _configured_path(value: str, config_path: Path, override: Path | None) -> Path:
    chosen = override if override is not None else Path(value)
    return chosen if chosen.is_absolute() else config_path.parent / chosen


def _store(config: MonitorConfig, config_path: Path, override: Path | None) -> RoleStore:
    return RoleStore(_configured_path(config.database, config_path, override))


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "demo":
        result = run_demo(
            _config_path(args.config),
            args.output_dir.expanduser().resolve(),
            fixtures_dir=args.fixtures_dir.expanduser().resolve() if args.fixtures_dir else None,
            reset=args.reset,
        )
        payload = {
            "mode": "offline synthetic demo",
            "synthetic_decision": "Synthetic demo decision; this is not a production approval.",
            "approved_role_keys": list(result.approved_role_keys),
            "database_path": result.database_path,
            "manifest_id": result.manifest["manifest_id"],
            "manifest_path": result.manifest_path,
            "receipt": result.receipt,
            "receipt_path": result.receipt_path,
            "task_count": result.task_count,
            "scan_summary": result.pipeline.summary,
            "final_state": result.final_state,
        }
        if args.telegram_dry_run:
            notification = build_scan_notification(result.pipeline.summary)
            payload["notification"] = DryRunNotifier("telegram").send(notification).as_dict()
        _print(payload)
        return 0

    config_path = _config_path(args.config)
    config = load_config(config_path)
    if args.command == "scan":
        if not args.allow_network:
            _parser().error("scan requires --allow-network before making live requests")
        telegram_credentials = None
        if args.notify_telegram:
            try:
                telegram_credentials = load_telegram_credentials()
            except ValueError as error:
                _parser().error(str(error))
        store = _store(config, config_path, args.database)
        try:
            output_dir = _configured_path(config.output_dir, config_path, args.output_dir)
            result = run_pipeline(config, UrllibTransport(), store, output_dir)
            payload = {
                "summary": result.summary,
                "reports": result.reports,
                "failures": list(result.failures),
            }
            status = 0
            if args.telegram_dry_run:
                notification = build_scan_notification(result.summary)
                payload["notification"] = DryRunNotifier("telegram").send(notification).as_dict()
            elif args.notify_telegram:
                notification = build_scan_notification(result.summary)
                try:
                    payload["notification"] = TelegramNotifier(telegram_credentials).send(notification).as_dict()
                except NotificationError as error:
                    payload["notification"] = {
                        "provider": "telegram",
                        "status": "failed",
                        "error": str(error),
                    }
                    status = 1
            _print(payload)
        finally:
            store.close()
        return status

    store = _store(config, config_path, args.database)
    try:
        review = ReviewService(store)
        if args.command == "queue":
            _print({"pending": review.queue(), "deferred": review.deferred(), "counts": store.state_counts()})
        elif args.command == "review":
            operation = getattr(review, {"approved": "approve", "rejected": "reject", "deferred": "defer"}[args.decision])
            _print(operation(args.role_key, args.note))
        elif args.command == "prepare-handoff":
            manifest = build_manifest(store)
            path = write_manifest_atomic(manifest, args.destination.expanduser().resolve())
            _print({"manifest": manifest, "path": str(path)})
        elif args.command == "acknowledge-receipt":
            manifest = json.loads(args.manifest.expanduser().resolve().read_text(encoding="utf-8"))
            receipt = json.loads(args.receipt.expanduser().resolve().read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not isinstance(receipt, dict):
                raise ValueError("Manifest and receipt files must each contain a JSON object")
            acknowledge_receipt(store, manifest, receipt)
            _print({"manifest_id": manifest["manifest_id"], "status": "verified and recorded"})
        elif args.command == "recover-handoff":
            if not args.confirm_downstream_reconciled:
                _parser().error("recover-handoff requires --confirm-downstream-reconciled")
            manifest = json.loads(args.manifest.expanduser().resolve().read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("Manifest file must contain a JSON object")
            recovery_status = store.recover_manifest(manifest)
            message = (
                "released for idempotent retry"
                if recovery_status == "pending"
                else "invalidated because a role became unavailable; re-scan before retrying"
            )
            _print({"manifest_id": manifest["manifest_id"], "status": message})
    finally:
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
