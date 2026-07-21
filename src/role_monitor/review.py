"""Explicit human-review operations for the role monitor."""

from __future__ import annotations

from typing import Any

from .store import RoleStore


class ReviewService:
    """Small facade that makes the approval boundary obvious to callers."""

    def __init__(self, store: RoleStore) -> None:
        self.store = store

    def queue(self) -> list[dict[str, Any]]:
        return self.store.pending_review()

    def deferred(self) -> list[dict[str, Any]]:
        return self.store.deferred_review()

    def approve(self, role_key: str, note: str = "") -> dict[str, Any]:
        return self.store.decide(role_key, "approved", note)

    def reject(self, role_key: str, note: str = "") -> dict[str, Any]:
        return self.store.decide(role_key, "rejected", note)

    def defer(self, role_key: str, note: str = "") -> dict[str, Any]:
        return self.store.decide(role_key, "deferred", note)
