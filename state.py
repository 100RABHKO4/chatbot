"""Versioned, thread-safe context store for the Vera bot.

Contract (challenge-testing-brief.md §2.1, examples/api-call-examples.md 1.5-1.6):
  * Contexts are keyed by (scope, context_id).
  * A strictly higher version replaces the stored one atomically.
  * The same or a lower version is rejected as ``stale_version`` (HTTP 409)
    and leaves state untouched.

Payloads are stored as parsed and treated as read-only by every caller.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

SCOPES = ("category", "merchant", "customer", "trigger")

# Field inside each payload that carries the entity's own id. The judge's
# context_id normally equals it, but the testing brief shows a short
# context_id ("m_001_drmeera") next to a long payload id, so both resolve.
PAYLOAD_ID_FIELD = {
    "category": "slug",
    "merchant": "merchant_id",
    "customer": "customer_id",
    "trigger": "id",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class StoredContext:
    scope: str
    context_id: str
    version: int
    payload: dict
    stored_at: str


@dataclass(frozen=True)
class PutResult:
    accepted: bool
    current: StoredContext  # the record that is authoritative after the call


class ContextStore:
    """In-memory store with secondary indexes for fast lookups."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, StoredContext]] = {s: {} for s in SCOPES}
        self._aliases: dict[str, dict[str, str]] = {s: {} for s in SCOPES}
        self._by_merchant: dict[str, dict[str, set[str]]] = {"customer": {}, "trigger": {}}

    # ------------------------------------------------------------------ writes
    def put(self, scope: str, context_id: str, version: int, payload: dict) -> PutResult:
        if scope not in SCOPES:
            raise ValueError(f"invalid scope: {scope!r}")
        with self._lock:
            current = self._records[scope].get(context_id)
            if current is not None and version <= current.version:
                return PutResult(accepted=False, current=current)

            record = StoredContext(scope, context_id, version, payload, utc_now_iso())
            if current is not None:
                self._unindex(current)
            self._records[scope][context_id] = record
            self._index(record)
            return PutResult(accepted=True, current=record)

    def clear(self) -> None:
        with self._lock:
            for scope in SCOPES:
                self._records[scope].clear()
                self._aliases[scope].clear()
            for index in self._by_merchant.values():
                index.clear()

    # ------------------------------------------------------------------- reads
    def get_record(self, scope: str, context_id: Optional[str]) -> Optional[StoredContext]:
        if scope not in SCOPES or not isinstance(context_id, str):
            return None
        with self._lock:
            records = self._records[scope]
            record = records.get(context_id)
            if record is None:
                alias_target = self._aliases[scope].get(context_id)
                if alias_target is not None:
                    record = records.get(alias_target)
            return record

    def get(self, scope: str, context_id: Optional[str]) -> Optional[dict]:
        record = self.get_record(scope, context_id)
        return record.payload if record is not None else None

    def category(self, slug: Optional[str]) -> Optional[dict]:
        return self.get("category", slug)

    def merchant(self, merchant_id: Optional[str]) -> Optional[dict]:
        return self.get("merchant", merchant_id)

    def customer(self, customer_id: Optional[str]) -> Optional[dict]:
        return self.get("customer", customer_id)

    def trigger(self, trigger_id: Optional[str]) -> Optional[dict]:
        return self.get("trigger", trigger_id)

    def customers_of(self, merchant_id: str) -> list[dict]:
        return self._related("customer", merchant_id)

    def triggers_of(self, merchant_id: str) -> list[dict]:
        return self._related("trigger", merchant_id)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {scope: len(self._records[scope]) for scope in SCOPES}

    # ---------------------------------------------------------------- internal
    def _related(self, scope: str, merchant_id: str) -> list[dict]:
        merchant_key = self._canonical_merchant_id(merchant_id)
        with self._lock:
            ids = sorted(self._by_merchant[scope].get(merchant_key, ()))
            return [self._records[scope][cid].payload for cid in ids]

    def _canonical_merchant_id(self, merchant_id: str) -> str:
        record = self.get_record("merchant", merchant_id)
        if record is not None:
            own_id = record.payload.get("merchant_id")
            if isinstance(own_id, str) and own_id:
                return own_id
        return merchant_id

    def _index(self, record: StoredContext) -> None:
        own_id = _payload_id(record)
        if own_id and own_id != record.context_id:
            self._aliases[record.scope][own_id] = record.context_id
        merchant_id = _payload_merchant_id(record)
        if merchant_id:
            self._by_merchant[record.scope].setdefault(merchant_id, set()).add(record.context_id)

    def _unindex(self, record: StoredContext) -> None:
        own_id = _payload_id(record)
        if own_id and self._aliases[record.scope].get(own_id) == record.context_id:
            del self._aliases[record.scope][own_id]
        merchant_id = _payload_merchant_id(record)
        if merchant_id:
            members = self._by_merchant[record.scope].get(merchant_id)
            if members is not None:
                members.discard(record.context_id)
                if not members:
                    del self._by_merchant[record.scope][merchant_id]


def _payload_id(record: StoredContext) -> Optional[str]:
    value: Any = record.payload.get(PAYLOAD_ID_FIELD[record.scope])
    return value if isinstance(value, str) and value else None


def _payload_merchant_id(record: StoredContext) -> Optional[str]:
    if record.scope not in ("customer", "trigger"):
        return None
    value: Any = record.payload.get("merchant_id")
    return value if isinstance(value, str) and value else None
