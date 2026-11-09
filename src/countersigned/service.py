"""The gate: wires the pure engine to Postgres.

Transaction boundaries are the design. Every state change and the audit entry that
records it commit together or not at all (so there is no "it happened but isn't
logged" and no "it's logged but didn't happen"). Locks, always taken in this order:

    7001 decision lock  ->  row locks (actions, kill_switch)  ->  7002 audit lock

* propose():   holds 7001 so cumulative-window arithmetic is exact under concurrency.
* approve():   locks the action row, binds to its hash, single use.
* worker:      claims an outbox row (SKIP LOCKED), takes the kill switch FOR SHARE
               first, re-evaluates, then acts inside the same transaction.
* kill switch: takes the row FOR UPDATE, so once it commits, no execution that
               commits afterwards can have missed it.
"""

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, UTC
from typing import Any
from collections.abc import Callable

from . import audit, effects
from .canonical import action_hash, canonical_json, iso, sha256_hex
from .db import Database
from .engine import (
    AgentInfo, Context, context_to_dict, evaluate, grant_from_dict, replay,
)
from .errors import Conflict, EffectBlocked, Expired, Forbidden, NotFound, Unauthorized
from .models import ActionProposal
from .telemetry import span

DECISION_LOCK = 7001
COUNTED = ("queued", "pending_approval", "approved", "executed")
STATUS_FOR = {
    "execute": "queued", "escalate": "pending_approval", "ask": "needs_info",
    "defer": "deferred", "refuse": "refused",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def policy_version_of(body: dict[str, Any]) -> str:
    return "p-" + sha256_hex(canonical_json(body))[:12]


class Gate:
    def __init__(self, db: Database, clock: Callable[[], datetime] = _utcnow) -> None:
        self.db = db
        self.clock = clock

    # ------------------------------------------------------------ principals

    def create_principal(
        self, id: str, kind: str, name: str, key: str, grants: list[dict[str, Any]] | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO principals (id, kind, name, key_hash, grants) VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind, name = EXCLUDED.name, "
                "key_hash = EXCLUDED.key_hash, grants = EXCLUDED.grants",
                (id, kind, name, hash_key(key), canonical_json(grants or [])),
            )

    def authenticate(self, key: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id, kind, name, disabled FROM principals WHERE key_hash = %s", (hash_key(key),)
            ).fetchone()
        if row is None or row["disabled"]:
            raise Unauthorized("unknown or disabled API key")
        return dict(row)

    def set_agent_disabled(self, agent_id: str, disabled: bool, actor: str) -> None:
        with self.db.transaction() as conn:
            n = conn.execute(
                "UPDATE principals SET disabled = %s WHERE id = %s AND kind = 'agent'", (disabled, agent_id)
            ).rowcount
            if n != 1:
                raise NotFound(f"no agent {agent_id}")
            audit.append(conn, "agent_disabled" if disabled else "agent_enabled", actor, {"agent": agent_id}, self.clock())

    def _agent(self, conn: Any, agent_id: str) -> AgentInfo:
        row = conn.execute(
            "SELECT id, grants, disabled FROM principals WHERE id = %s AND kind = 'agent'", (agent_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"no agent {agent_id}")
        return AgentInfo(row["id"], row["disabled"], tuple(grant_from_dict(g) for g in row["grants"]))

    # ---------------------------------------------------------------- policy

    def activate_policy(self, body: dict[str, Any], actor: str = "system") -> str:
        version = policy_version_of(body)
        with self.db.transaction() as conn:
            cur = conn.execute("SELECT version FROM policies WHERE active").fetchone()
            if cur and cur["version"] == version:
                return version
            conn.execute(
                "INSERT INTO policies (version, body) VALUES (%s, %s::jsonb) ON CONFLICT DO NOTHING",
                (version, canonical_json(body)),
            )
            conn.execute("UPDATE policies SET active = false WHERE active")
            conn.execute("UPDATE policies SET active = true WHERE version = %s", (version,))
            audit.append(conn, "policy_activated", actor, {"version": version, "previous": cur["version"] if cur else None}, self.clock())
        return version

    def active_policy(self, conn: Any) -> tuple[dict[str, Any], str]:
        row = conn.execute("SELECT version, body FROM policies WHERE active").fetchone()
        if row is None:
            raise Conflict("no active policy")
        return row["body"], row["version"]

    def get_policy(self, version: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT body FROM policies WHERE version = %s", (version,)).fetchone()
        if row is None:
            raise NotFound(f"no policy {version}")
        return row["body"]

    # ----------------------------------------------------------- context

    def _facts(self, conn: Any, operation: str, p: dict[str, Any]) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        tid = p.get("ticket_id")
        if isinstance(tid, str):
            t = conn.execute("SELECT kind, status FROM sim_tickets WHERE id = %s", (tid,)).fetchone()
            if t:
                facts["tickets"] = {tid: {"kind": t["kind"], "status": t["status"]}}
        seg = p.get("segment")
        if operation == "customer_data.delete_records" and isinstance(seg, str):
            s = conn.execute(
                "SELECT count(*) FILTER (WHERE deleted_at IS NULL) AS live, count(*) AS total "
                "FROM sim_customers WHERE segment = %s", (seg,),
            ).fetchone()
            if s["total"] > 0:
                facts["segment_sizes"] = {seg: int(s["live"])}
        pid = p.get("payment_id")
        if operation == "payments.refund" and isinstance(pid, str):
            pay = conn.execute("SELECT amount_cents, refunded_cents FROM sim_payments WHERE id = %s", (pid,)).fetchone()
            if pay:
                facts["payments"] = {pid: {"amount_cents": int(pay["amount_cents"]), "refunded_cents": int(pay["refunded_cents"])}}
        svc = p.get("service")
        if operation == "infrastructure.scale_down" and isinstance(svc, str):
            s = conn.execute("SELECT replicas FROM sim_services WHERE name = %s", (svc,)).fetchone()
            if s:
                facts["services"] = {svc: int(s["replicas"])}
        return facts

    def _usage(
        self, conn: Any, agent_id: str, operation: str, hours: int, now: datetime,
        before_seq: int | None = None, exclude_id: str | None = None,
    ) -> tuple[int, int]:
        sql = (
            "SELECT COALESCE(SUM(quantity) FILTER (WHERE agent_id = %s), 0) AS a, COALESCE(SUM(quantity), 0) AS g "
            "FROM actions WHERE operation = %s AND created_at > %s AND status = ANY(%s)"
        )
        params: list[Any] = [agent_id, operation, now - timedelta(hours=hours), list(COUNTED)]
        if before_seq is not None:
            sql += " AND seq < %s"
            params.append(before_seq)
        if exclude_id is not None:
            sql += " AND id <> %s"
            params.append(exclude_id)
        row = conn.execute(sql, params).fetchone()
        return int(row["a"]), int(row["g"])

    def _context(
        self, conn: Any, agent: AgentInfo, policy: dict[str, Any], version: str, operation: str,
        params: dict[str, Any], now: datetime, *, mode: str, human_approved: bool = False,
        before_seq: int | None = None, exclude_id: str | None = None,
    ) -> Context:
        ks = conn.execute("SELECT active, reason FROM kill_switch WHERE id = 1").fetchone()
        spec = policy["operations"].get(operation, {})
        hours = spec.get("window", {}).get("hours", 24)
        ua, ug = self._usage(conn, agent.id, operation, hours, now, before_seq, exclude_id)
        return Context(
            now=now, agent=agent, policy=policy, policy_version=version,
            facts=self._facts(conn, operation, params), usage_agent=ua, usage_global=ug,
            kill_switch=ks["active"], kill_reason=ks["reason"], mode=mode, human_approved=human_approved,
        )

    # --------------------------------------------------------------- propose

    def propose(self, agent_id: str, proposal: ActionProposal) -> dict[str, Any]:
        with span("gate.propose", agent=agent_id, operation=proposal.operation) as sp:
            with self.db.transaction() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (DECISION_LOCK,))
                now = self.clock()
                a_hash = action_hash(agent_id, proposal.operation, proposal.params)
                if proposal.idempotency_key:
                    dup = conn.execute(
                        "SELECT * FROM actions WHERE agent_id = %s AND idempotency_key = %s",
                        (agent_id, proposal.idempotency_key),
                    ).fetchone()
                    if dup:
                        if dup["action_hash"] != a_hash:
                            raise Conflict("idempotency key was already used for a different action")
                        rec = self._record(dup)
                        rec["duplicate"] = True
                        return rec
                agent = self._agent(conn, agent_id)
                policy, version = self.active_policy(conn)
                ctx = self._context(conn, agent, policy, version, proposal.operation, proposal.params, now, mode="propose")
                dec = evaluate(proposal.operation, proposal.params, ctx)
                status = STATUS_FOR[dec.outcome]
                action_id = _new_id("act")
                not_before = datetime.fromisoformat(dec.retry_after.replace("Z", "+00:00")) if dec.retry_after else None
                conn.execute(
                    "INSERT INTO actions (id, agent_id, operation, params, action_hash, idempotency_key, status, decision, "
                    "policy_version, quantity, not_before, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)",
                    (action_id, agent_id, proposal.operation, canonical_json(proposal.params), a_hash,
                     proposal.idempotency_key, status, canonical_json(dec.to_dict()), version,
                     dec.effective_quantity or 0, not_before, now, now),
                )
                audit.append(conn, "decision", agent_id, {
                    "action_id": action_id, "operation": proposal.operation, "params": proposal.params,
                    "action_hash": a_hash, "status": status, "decision": dec.to_dict(),
                    "inputs": context_to_dict(ctx),
                }, now, action_id)
                if status == "queued":
                    self._enqueue(conn, action_id, now)
                row = conn.execute("SELECT * FROM actions WHERE id = %s", (action_id,)).fetchone()
            sp.set_attribute("countersigned.outcome", dec.outcome)
            sp.set_attribute("countersigned.risk_score", dec.risk_score)
            return self._record(row)

    def record_malformed(self, agent_id: str, raw: Any, error: str) -> None:
        """Audit a proposal that never became an action. Stores a hash, not the content."""
        digest = sha256_hex(raw if isinstance(raw, str) else canonical_json(raw) if isinstance(raw, (dict, list)) else repr(raw))
        with self.db.transaction() as conn:
            audit.append(conn, "malformed_proposal", agent_id, {"raw_sha256": digest, "error": error[:300]}, self.clock())

    def _enqueue(self, conn: Any, action_id: str, now: datetime) -> None:
        conn.execute(
            "INSERT INTO outbox (action_id, topic, created_at) VALUES (%s, 'execute', %s)", (action_id, now)
        )

    @staticmethod
    def _record(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "agent_id": row["agent_id"], "operation": row["operation"], "params": row["params"],
            "action_hash": row["action_hash"], "status": row["status"], "decision": row["decision"],
            "policy_version": row["policy_version"], "not_before": iso(row["not_before"]) if row["not_before"] else None,
            "created_at": iso(row["created_at"]), "updated_at": iso(row["updated_at"]),
        }

    def get_action(self, action_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id = %s", (action_id,)).fetchone()
        if row is None:
            raise NotFound(f"no action {action_id}")
        return self._record(row)

    def list_actions(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if status:
                rows = conn.execute("SELECT * FROM actions WHERE status = %s ORDER BY seq DESC LIMIT %s", (status, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM actions ORDER BY seq DESC LIMIT %s", (limit,)).fetchall()
        return [self._record(r) for r in rows]

    # ------------------------------------------------------------- approvals

    def _approver(self, conn: Any, approver_id: str) -> None:
        row = conn.execute("SELECT kind, disabled FROM principals WHERE id = %s", (approver_id,)).fetchone()
        if row is None or row["disabled"] or row["kind"] not in ("approver", "admin"):
            raise Forbidden(f"{approver_id} cannot approve actions")

    def decide(
        self, action_id: str, approver_id: str, verdict: str, presented_hash: str, note: str | None = None,
    ) -> dict[str, Any]:
        """Approve or reject. `presented_hash` must be the hash of the action the approver was shown."""
        expired = False
        with span("gate.decide", action=action_id, verdict=verdict), self.db.transaction() as conn:
            now = self.clock()
            row = conn.execute("SELECT * FROM actions WHERE id = %s FOR UPDATE", (action_id,)).fetchone()
            if row is None:
                raise NotFound(f"no action {action_id}")
            self._approver(conn, approver_id)
            if row["agent_id"] == approver_id:
                raise Forbidden("an agent cannot approve its own action")
            if row["status"] != "pending_approval":
                raise Conflict(f"action is {row['status']}, not pending_approval")
            if presented_hash != row["action_hash"]:
                raise Conflict("the hash presented does not match this action: approve exactly what was proposed")
            policy, _ = self.active_policy(conn)
            ttl = timedelta(minutes=policy["approval_ttl_minutes"])
            if now > row["created_at"] + ttl:
                conn.execute("UPDATE actions SET status = 'expired', updated_at = %s WHERE id = %s", (now, action_id))
                audit.append(conn, "approval_expired", "system", {"action_id": action_id}, now, action_id)
                expired = True
            else:
                conn.execute(
                    "INSERT INTO approvals (id, action_id, action_hash, approver_id, verdict, note, created_at, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (_new_id("apr"), action_id, row["action_hash"], approver_id, verdict, note, now, now + ttl),
                )
                new_status = "approved" if verdict == "approve" else "rejected"
                conn.execute("UPDATE actions SET status = %s, updated_at = %s WHERE id = %s", (new_status, now, action_id))
                audit.append(conn, f"action_{new_status}", approver_id, {
                    "action_id": action_id, "action_hash": row["action_hash"], "note": note,
                }, now, action_id)
                if new_status == "approved":
                    self._enqueue(conn, action_id, now)
                row = conn.execute("SELECT * FROM actions WHERE id = %s", (action_id,)).fetchone()
        if expired:
            raise Expired("the approval window has passed; the action is expired and must be re-proposed")
        return self._record(row)

    def expire_stale(self) -> int:
        with self.db.transaction() as conn:
            now = self.clock()
            policy, _ = self.active_policy(conn)
            cutoff = now - timedelta(minutes=policy["approval_ttl_minutes"])
            rows = conn.execute(
                "SELECT id FROM actions WHERE status = 'pending_approval' AND created_at < %s FOR UPDATE", (cutoff,)
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE actions SET status = 'expired', updated_at = %s WHERE id = %s", (now, r["id"]))
                audit.append(conn, "approval_expired", "system", {"action_id": r["id"]}, now, r["id"])
            return len(rows)

    # ----------------------------------------------------------- kill switch

    def set_kill_switch(self, active: bool, actor: str, reason: str | None = None) -> dict[str, Any]:
        with self.db.transaction() as conn:
            now = self.clock()
            conn.execute("SELECT 1 FROM kill_switch WHERE id = 1 FOR UPDATE")
            conn.execute(
                "UPDATE kill_switch SET active = %s, reason = %s, set_by = %s, set_at = %s WHERE id = 1",
                (active, reason if active else None, actor, now),
            )
            audit.append(conn, "kill_switch_on" if active else "kill_switch_off", actor, {"reason": reason}, now)
        return self.kill_switch()

    def kill_switch(self) -> dict[str, Any]:
        with self.db.transaction() as conn:
            r = conn.execute("SELECT active, reason, set_by, set_at FROM kill_switch WHERE id = 1").fetchone()
        return {"active": r["active"], "reason": r["reason"], "set_by": r["set_by"], "set_at": iso(r["set_at"]) if r["set_at"] else None}

    # -------------------------------------------------------- deferred release

    def release_due(self) -> int:
        """Re-decide deferred actions whose time has come."""
        released = 0
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (DECISION_LOCK,))
            now = self.clock()
            rows = conn.execute(
                "SELECT * FROM actions WHERE status = 'deferred' AND not_before <= %s ORDER BY seq FOR UPDATE", (now,)
            ).fetchall()
            for a in rows:
                agent = self._agent(conn, a["agent_id"])
                policy, version = self.active_policy(conn)
                ctx = self._context(conn, agent, policy, version, a["operation"], a["params"], now, mode="propose", exclude_id=a["id"])
                dec = evaluate(a["operation"], a["params"], ctx)
                status = STATUS_FOR[dec.outcome]
                nb = datetime.fromisoformat(dec.retry_after.replace("Z", "+00:00")) if dec.retry_after else None
                conn.execute(
                    "UPDATE actions SET status = %s, decision = %s::jsonb, policy_version = %s, quantity = %s, "
                    "not_before = %s, updated_at = %s WHERE id = %s",
                    (status, canonical_json(dec.to_dict()), version, dec.effective_quantity or 0, nb, now, a["id"]),
                )
                audit.append(conn, "redecision", "system", {
                    "action_id": a["id"], "operation": a["operation"], "params": a["params"], "action_hash": a["action_hash"],
                    "status": status, "decision": dec.to_dict(), "inputs": context_to_dict(ctx),
                }, now, a["id"])
                if status == "queued":
                    self._enqueue(conn, a["id"], now)
                released += 1
        return released

    # ---------------------------------------------------------------- worker

    def run_worker_once(self) -> dict[str, Any] | None:
        """Process one outbox row. Returns None when the queue is empty."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id, action_id FROM outbox WHERE processed_at IS NULL ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
            ).fetchone()
            if row is None:
                return None
            # Kill switch first: FOR SHARE here pairs with FOR UPDATE in set_kill_switch.
            ks = conn.execute("SELECT active, reason FROM kill_switch WHERE id = 1 FOR SHARE").fetchone()
            out = self._execute(conn, row["action_id"], ks)
            conn.execute("UPDATE outbox SET processed_at = %s, attempts = attempts + 1 WHERE id = %s", (self.clock(), row["id"]))
            return out

    def run_worker_until_empty(self, limit: int = 1000) -> list[dict[str, Any]]:
        out = []
        for _ in range(limit):
            r = self.run_worker_once()
            if r is None:
                break
            out.append(r)
        return out

    def _execute(self, conn: Any, action_id: str, ks: dict[str, Any]) -> dict[str, Any]:
        with span("gate.execute", action=action_id) as sp:
            a = conn.execute("SELECT * FROM actions WHERE id = %s FOR UPDATE", (action_id,)).fetchone()
            now = self.clock()
            if a is None or a["status"] not in ("queued", "approved"):
                audit.append(conn, "duplicate_delivery_ignored", "worker", {"action_id": action_id, "status": a["status"] if a else None}, now, action_id)
                return {"action_id": action_id, "result": "ignored"}

            agent = self._agent(conn, a["agent_id"])
            policy, version = self.active_policy(conn)
            human_ok = False
            approval = None
            if a["status"] == "approved":
                approval = conn.execute(
                    "SELECT * FROM approvals WHERE action_id = %s AND verdict = 'approve' FOR UPDATE", (action_id,)
                ).fetchone()
                human_ok = bool(
                    approval and approval["action_hash"] == a["action_hash"]
                    and approval["consumed_at"] is None and approval["expires_at"] > now
                )
            ctx = self._context(
                conn, agent, policy, version, a["operation"], a["params"], now, mode="execute",
                human_approved=human_ok, before_seq=a["seq"], exclude_id=action_id,
            )
            # The kill switch we hold FOR SHARE is authoritative for this transaction.
            ctx = replace(ctx, kill_switch=ks["active"], kill_reason=ks["reason"])
            dec = evaluate(a["operation"], a["params"], ctx)
            checked = {"outcome": dec.outcome, "risk_score": dec.risk_score, "policy_version": version, "reasons": dec.reasons}

            if a["status"] == "approved" and not human_ok:
                return self._block(conn, a, now, checked, ctx, "approval is missing, altered, used or expired")
            if dec.outcome != "execute":
                return self._block(conn, a, now, checked, ctx, "; ".join(dec.reasons))

            try:
                with conn.transaction():  # savepoint: an effect that fails leaves no partial change
                    conn.execute(
                        "INSERT INTO executions (action_id, approval_id, started_at, result) VALUES (%s, %s, %s, '{}'::jsonb)",
                        (action_id, approval["id"] if approval else None, now),
                    )
                    if approval:
                        n = conn.execute(
                            "UPDATE approvals SET consumed_at = %s WHERE id = %s AND consumed_at IS NULL", (now, approval["id"])
                        ).rowcount
                        if n != 1:
                            raise EffectBlocked("approval was already used")
                    result = effects.apply(conn, a["operation"], a["params"], action_id, now)
                    conn.execute("UPDATE executions SET result = %s::jsonb WHERE action_id = %s", (canonical_json(result), action_id))
            except EffectBlocked as e:
                return self._block(conn, a, now, checked, ctx, str(e))
            except Exception as e:
                conn.execute("UPDATE actions SET status = 'failed', updated_at = %s WHERE id = %s", (now, action_id))
                audit.append(conn, "execution_failed", "worker", {"action_id": action_id, "error": type(e).__name__}, now, action_id)
                sp.set_attribute("countersigned.result", "failed")
                return {"action_id": action_id, "result": "failed", "error": type(e).__name__}

            conn.execute("UPDATE actions SET status = 'executed', updated_at = %s WHERE id = %s", (now, action_id))
            audit.append(conn, "executed", "worker", {
                "action_id": action_id, "result": result, "recheck": checked,
                "approval_id": approval["id"] if approval else None, "inputs": context_to_dict(ctx),
            }, now, action_id)
            sp.set_attribute("countersigned.result", "executed")
            return {"action_id": action_id, "result": "executed", "effect": result}

    def _block(self, conn: Any, a: dict[str, Any], now: datetime, checked: dict[str, Any], ctx: Context, why: str) -> dict[str, Any]:
        conn.execute("UPDATE actions SET status = 'blocked', updated_at = %s WHERE id = %s", (now, a["id"]))
        audit.append(conn, "execution_blocked", "worker", {
            "action_id": a["id"], "why": why, "recheck": checked, "inputs": context_to_dict(ctx),
        }, now, a["id"])
        return {"action_id": a["id"], "result": "blocked", "why": why}

    # ----------------------------------------------------------------- audit

    def audit_page(self, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT seq, ts, event_type, action_id, actor, payload, prev_hash, hash FROM audit_log "
                "WHERE seq > %s ORDER BY seq LIMIT %s", (after_seq, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def verify_audit(self) -> dict[str, Any]:
        with self.db.transaction() as conn:
            return audit.verify(conn).to_dict()

    def replay_decision(self, action_id: str) -> dict[str, Any]:
        """Re-run the stored decision from the audit log and compare. Same inputs, same answer."""
        with self.db.transaction() as conn:
            ev = conn.execute(
                "SELECT payload FROM audit_log WHERE action_id = %s AND event_type IN ('decision', 'redecision') "
                "ORDER BY seq DESC LIMIT 1", (action_id,),
            ).fetchone()
            if ev is None:
                raise NotFound(f"no decision recorded for {action_id}")
            p = ev["payload"]
            version = p["decision"]["policy_version"]
            body = conn.execute("SELECT body FROM policies WHERE version = %s", (version,)).fetchone()["body"]
        again = replay(p["operation"], p["params"], p["inputs"], body, version).to_dict()
        return {"matches": again == p["decision"], "recorded": p["decision"], "replayed": again}
