"""Canonical JSON and hashing.

Everything that gets hashed (action identity, policy versions, audit entries) goes
through `canonical_json`: sorted keys, no whitespace, UTF-8, integers only. The
TypeScript playground implements the same function, and the conformance tests
check that both produce identical hashes.
"""

import hashlib
import json
from datetime import datetime, UTC
from typing import Any

GENESIS = "0" * 64


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def action_hash(agent_id: str, operation: str, params: dict[str, Any]) -> str:
    """Identity of exactly what was proposed. Approvals are bound to this."""
    return sha256_hex(canonical_json({"agent": agent_id, "operation": operation, "params": params}))


def iso(ts: datetime) -> str:
    """UTC, millisecond precision, trailing Z (same shape as JS Date.toISOString)."""
    if ts.tzinfo is None:
        raise ValueError("naive datetime")
    u = ts.astimezone(UTC)
    return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
