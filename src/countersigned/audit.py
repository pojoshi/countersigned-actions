"""Hash-chained audit log.

Each entry commits to the one before it:

    hash_n = sha256( hash_{n-1} + "\\n" + canonical_json({seq, ts, event_type, action_id, actor, payload}) )

so changing, deleting or reordering any earlier entry changes every hash after it.
The database also refuses UPDATE / DELETE / TRUNCATE, but a superuser can switch
triggers off. This is tamper-*evident*: `verify` finds the first bad entry, and the
head hash can be copied somewhere the database owner cannot reach (a ticket, a
printed page, another system) to catch a rewrite of the whole chain.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from collections.abc import Iterable

from .canonical import GENESIS, canonical_json, iso, sha256_hex


def chain_hash(
    prev: str, seq: int, ts: str, event_type: str, action_id: str | None, actor: str, payload: Any,
) -> str:
    body = canonical_json(
        {"seq": seq, "ts": ts, "event_type": event_type, "action_id": action_id, "actor": actor, "payload": payload}
    )
    return sha256_hex(prev + "\n" + body)


def append(
    conn: Any, event_type: str, actor: str, payload: dict[str, Any], now: datetime, action_id: str | None = None,
) -> dict[str, Any]:
    """Append one entry. Call inside the same transaction as the change it records."""
    conn.execute("SELECT pg_advisory_xact_lock(7002)")  # serialises appends so the chain has no forks
    last = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    seq = last["seq"] + 1 if last else 1
    prev = last["hash"] if last else GENESIS
    ts = iso(now)
    h = chain_hash(prev, seq, ts, event_type, action_id, actor, payload)
    conn.execute(
        "INSERT INTO audit_log (seq, ts, event_type, action_id, actor, payload, prev_hash, hash) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
        (seq, ts, event_type, action_id, actor, canonical_json(payload), prev, h),
    )
    return {"seq": seq, "hash": h}


@dataclass
class VerifyResult:
    ok: bool
    entries: int
    head: str
    first_bad_seq: int | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "entries": self.entries, "head": self.head,
            "first_bad_seq": self.first_bad_seq, "reason": self.reason,
        }


def verify_rows(rows: Iterable[dict[str, Any]]) -> VerifyResult:
    prev, n = GENESIS, 0
    for r in rows:
        n += 1
        if r["seq"] != n:
            return VerifyResult(False, n - 1, prev, r["seq"], f"expected seq {n}, found {r['seq']} (entry removed or reordered)")
        if r["prev_hash"] != prev:
            return VerifyResult(False, n - 1, prev, r["seq"], "prev_hash does not match the previous entry")
        h = chain_hash(prev, r["seq"], r["ts"], r["event_type"], r["action_id"], r["actor"], r["payload"])
        if h != r["hash"]:
            return VerifyResult(False, n - 1, prev, r["seq"], "entry content does not match its hash (edited)")
        prev = h
    return VerifyResult(True, n, prev)


def verify(conn: Any) -> VerifyResult:
    rows = conn.execute(
        "SELECT seq, ts, event_type, action_id, actor, payload, prev_hash, hash FROM audit_log ORDER BY seq"
    ).fetchall()
    return verify_rows(rows)
