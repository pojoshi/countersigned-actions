"""The decision engine: a pure function.

    evaluate(operation, params, context) -> Decision

No I/O, no clock, no randomness, no model. Everything the decision depends on is in
`Context`, and the service stores that context next to the decision, so any decision
can be replayed from the audit log and must come out identical (see `replay`).

Outcomes, in precedence order: refuse > ask > defer > escalate > execute.

  refuse    policy forbids it (no grant, kill switch, hard limit, bad environment ...)
  ask       the proposal is incomplete or contradicts verified facts; fix and re-propose
  defer     not now (outside a change window); retry_after says when
  escalate  a human must approve this exact action
  execute   allowed, within every limit

`confidence` is not a probability. It is 100 minus penalties for things a reviewer
should know: the agent's claim disagreed with verified data, the environment was
assumed, or the action sits right under a limit. `risk_score` is a 0-100 sum of
reversibility, magnitude, PII, environment, cumulative pressure and claim mismatch.
In the default policy the limits decide; the risk score is the explanation and a
backstop for policies with looser limits.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from typing import Any
from collections.abc import Callable

from .canonical import iso

REV_POINTS = {"reversible": 0, "partial": 15, "irreversible": 35}
ENVIRONMENTS = ("staging", "prod")


# ----------------------------------------------------------------------- inputs

@dataclass(frozen=True)
class Grant:
    operation: str
    environments: tuple[str, ...]
    max_quantity: int | None = None


@dataclass(frozen=True)
class AgentInfo:
    id: str
    disabled: bool
    grants: tuple[Grant, ...]


@dataclass(frozen=True)
class Context:
    now: datetime
    agent: AgentInfo
    policy: dict[str, Any]
    policy_version: str
    facts: dict[str, Any]
    usage_agent: int = 0
    usage_global: int = 0
    kill_switch: bool = False
    kill_reason: str | None = None
    mode: str = "propose"  # "propose" | "execute" (re-check just before acting)
    human_approved: bool = False


@dataclass
class Decision:
    outcome: str
    confidence: int
    risk_score: int
    reversibility: str
    effective_quantity: int | None
    environment: str
    evidence: list[dict[str, str]]
    missing_information: list[str]
    reasons: list[str]
    retry_after: str | None
    policy_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome, "confidence": self.confidence, "risk_score": self.risk_score,
            "reversibility": self.reversibility, "effective_quantity": self.effective_quantity,
            "environment": self.environment, "evidence": self.evidence,
            "missing_information": self.missing_information, "reasons": self.reasons,
            "retry_after": self.retry_after, "policy_version": self.policy_version,
        }


def grant_from_dict(d: dict[str, Any]) -> Grant:
    mq = d.get("max_quantity")
    return Grant(d["operation"], tuple(d.get("environments", [])), mq if isinstance(mq, int) else None)


# -------------------------------------------------------------------- resolvers
# A resolver reads the operation's parameters and the verified facts and returns the
# *effective* quantity (the blast radius the system believes in, which can be larger
# than what the agent claimed), the reversibility, and anything missing or wrong.

@dataclass
class Resolved:
    quantity: int | None = None
    reversibility: str = "reversible"
    evidence: list[dict[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    inconsistent: bool = False


def _ev(rule: str, detail: str, source: str) -> dict[str, str]:
    return {"rule": rule, "detail": detail, "source": source}


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _pos_int(p: dict[str, Any], key: str, r: Resolved) -> int | None:
    if key not in p:
        return None  # reported by the required-fields check
    v = p[key]
    if not _is_int(v) or v <= 0:
        r.missing.append(f"{key} must be a positive integer")
        return None
    return v


def _check_ticket(p: dict[str, Any], facts: dict[str, Any], spec: dict[str, Any], r: Resolved) -> None:
    tid = p.get("ticket_id")
    if not isinstance(tid, str) or not tid:
        return
    t = facts.get("tickets", {}).get(tid)
    if t is None:
        r.missing.append(f"ticket {tid} does not exist: cite a real, open ticket")
        r.inconsistent = True
        r.evidence.append(_ev("ticket_verified", f"{tid} not found", "fact"))
    elif t.get("status") != "open" or t.get("kind") != spec.get("ticket_kind"):
        r.missing.append(f"ticket {tid} is {t.get('status')} / {t.get('kind')}; need an open {spec.get('ticket_kind')}")
        r.inconsistent = True
        r.evidence.append(_ev("ticket_verified", f"{tid} is {t.get('status')} {t.get('kind')}", "fact"))
    else:
        r.evidence.append(_ev("ticket_verified", f"{tid} is an open {t['kind']}", "fact"))


def _resolve_delete(p: dict[str, Any], facts: dict[str, Any], spec: dict[str, Any]) -> Resolved:
    r = Resolved()
    claimed = _pos_int(p, "count", r)
    hard = p.get("hard", False)
    if hard is True:
        r.reversibility = "irreversible"
        r.evidence.append(_ev("reversibility", "hard delete is permanent", "policy"))
    elif hard is False:
        r.reversibility = "reversible"
        r.evidence.append(_ev("reversibility", "soft delete can be restored", "policy"))
    else:
        r.missing.append("hard must be true or false")
    seg = p.get("segment")
    if "segment" in p and (not isinstance(seg, str) or not seg):
        r.missing.append("segment must be a non-empty string")
    elif isinstance(seg, str):
        size = facts.get("segment_sizes", {}).get(seg)
        if size is None:
            r.missing.append(f"segment '{seg}' is not a known segment")
            r.evidence.append(_ev("segment_resolved", f"'{seg}' unknown", "fact"))
        else:
            r.evidence.append(_ev("segment_resolved", f"segment '{seg}' currently matches {size} records", "fact"))
            if claimed is not None:
                r.quantity = max(claimed, size)
                if claimed != size:
                    r.inconsistent = True
                    r.missing.append(f"count must equal the {size} records segment '{seg}' matches (agent said {claimed})")
                    r.evidence.append(_ev("claim_mismatch", f"agent said {claimed}, data says {size}", "agent_claim"))
    if r.quantity is None:
        r.quantity = claimed
    _check_ticket(p, facts, spec, r)
    return r


def _resolve_export(p: dict[str, Any], facts: dict[str, Any], spec: dict[str, Any]) -> Resolved:
    r = Resolved()
    r.quantity = _pos_int(p, "count", r)
    dest = p.get("destination")
    d = spec.get("destinations", {})
    if isinstance(dest, str) and dest:
        if dest in d.get("internal", []):
            r.reversibility = "reversible"
            r.evidence.append(_ev("destination", f"{dest} is inside the perimeter", "policy"))
        elif dest in d.get("external", []):
            r.reversibility = "irreversible"
            r.evidence.append(_ev("destination", f"{dest} is external: data cannot be recalled", "policy"))
        else:
            r.reversibility = "irreversible"
            r.refusals.append(f"destination '{dest}' is not on the allow-list")
            r.evidence.append(_ev("destination", f"{dest} is not allow-listed", "policy"))
    elif "destination" in p:
        r.missing.append("destination must be a non-empty string")
    _check_ticket(p, facts, spec, r)
    return r


def _resolve_refund(p: dict[str, Any], facts: dict[str, Any], spec: dict[str, Any]) -> Resolved:
    r = Resolved(reversibility="partial")
    r.quantity = _pos_int(p, "amount_cents", r)
    r.evidence.append(_ev("reversibility", "money out can only be partly recovered", "policy"))
    pid = p.get("payment_id")
    if isinstance(pid, str) and pid:
        pay = facts.get("payments", {}).get(pid)
        if pay is None:
            r.missing.append(f"payment {pid} does not exist")
            r.inconsistent = True
            r.evidence.append(_ev("payment_verified", f"{pid} not found", "fact"))
        else:
            left = pay["amount_cents"] - pay["refunded_cents"]
            r.evidence.append(_ev("payment_verified", f"{pid} has {left} cents left to refund", "fact"))
            if r.quantity is not None and r.quantity > left:
                r.refusals.append(f"refund of {r.quantity} exceeds the {left} cents still refundable on {pid}")
    elif "payment_id" in p:
        r.missing.append("payment_id must be a non-empty string")
    _check_ticket(p, facts, spec, r)
    return r


def _resolve_transfer(p: dict[str, Any], facts: dict[str, Any], spec: dict[str, Any]) -> Resolved:
    r = Resolved(reversibility="irreversible")
    r.quantity = _pos_int(p, "amount_cents", r)
    r.evidence.append(_ev("reversibility", "transfers are final", "policy"))
    if "to_account" in p and (not isinstance(p["to_account"], str) or not p["to_account"]):
        r.missing.append("to_account must be a non-empty string")
    return r


def _resolve_scale_down(p: dict[str, Any], facts: dict[str, Any], spec: dict[str, Any]) -> Resolved:
    r = Resolved(reversibility="reversible")
    r.evidence.append(_ev("reversibility", "replicas can be scaled back up", "policy"))
    frm, to = p.get("replicas_from"), p.get("replicas_to")
    ok = True
    if "replicas_from" in p and (not _is_int(frm) or frm < 0):
        r.missing.append("replicas_from must be a non-negative integer")
        ok = False
    if "replicas_to" in p and (not _is_int(to) or to < 0):
        r.missing.append("replicas_to must be a non-negative integer")
        ok = False
    if ok and _is_int(frm) and _is_int(to):
        if to >= frm:
            r.missing.append("replicas_to must be lower than replicas_from")
        else:
            r.quantity = frm - to
    svc = p.get("service")
    if isinstance(svc, str) and svc:
        actual = facts.get("services", {}).get(svc)
        if actual is None:
            r.missing.append(f"service '{svc}' does not exist")
            r.inconsistent = True
        else:
            r.evidence.append(_ev("service_verified", f"{svc} currently runs {actual} replicas", "fact"))
            if _is_int(frm) and actual != frm:
                r.missing.append(f"{svc} runs {actual} replicas, not {frm}")
                r.inconsistent = True
                r.evidence.append(_ev("claim_mismatch", f"agent said {frm}, service has {actual}", "agent_claim"))
    elif "service" in p:
        r.missing.append("service must be a non-empty string")
    return r


RESOLVERS: dict[str, Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Resolved]] = {
    "customer_data.delete_records": _resolve_delete,
    "customer_data.export_records": _resolve_export,
    "payments.refund": _resolve_refund,
    "payments.transfer": _resolve_transfer,
    "infrastructure.scale_down": _resolve_scale_down,
}


# ------------------------------------------------------------------------ engine

def _next_window_start(now: datetime, start_hour: int) -> datetime:
    t = now.astimezone(UTC).replace(hour=start_hour, minute=0, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def risk_score(
    spec: dict[str, Any], reversibility: str, qty: int, env: str, usage_agent: int, inconsistent: bool,
) -> int:
    pts = REV_POINTS[reversibility]
    auto = spec["limits"]["auto"]
    if qty > 0:
        pts += 30 if auto == 0 else (30 * min(qty, 4 * auto)) // (4 * auto)
    if spec.get("pii") and qty > 0:
        pts += 10
    if env == "prod":
        pts += 10
    hard = spec.get("window", {}).get("agent_hard")
    if hard:
        pts += (20 * min(usage_agent + qty, hard)) // hard
    if inconsistent:
        pts += 10
    return min(100, pts)


def evaluate(operation: str, params: dict[str, Any], ctx: Context) -> Decision:
    ev: list[dict[str, str]] = []
    refusals: list[str] = []
    missing: list[str] = []
    defers: list[str] = []
    escalations: list[str] = []
    retry_after: str | None = None

    if ctx.kill_switch:
        refusals.append("kill switch is on" + (f": {ctx.kill_reason}" if ctx.kill_reason else ""))
        ev.append(_ev("kill_switch", "all actions are halted", "state"))
    if ctx.agent.disabled:
        refusals.append(f"agent {ctx.agent.id} is disabled")
        ev.append(_ev("agent_enabled", f"{ctx.agent.id} is disabled", "state"))

    spec = ctx.policy["operations"].get(operation)
    if spec is None or operation not in RESOLVERS:
        refusals.append(f"unknown operation '{operation}'")
        ev.append(_ev("known_operation", f"'{operation}' is not in policy {ctx.policy_version}", "policy"))
        return Decision("refuse", 95, 100, "irreversible", None, "prod", ev, [], refusals, None, ctx.policy_version)

    # identity and scope
    grant = next((g for g in ctx.agent.grants if g.operation == operation), None)
    if grant is None:
        refusals.append(f"{ctx.agent.id} has no grant for {operation}")
        ev.append(_ev("grant", f"no grant for {operation}", "state"))

    env_given = "environment" in params
    env = params.get("environment", "prod")
    if env not in ENVIRONMENTS:
        refusals.append("environment must be 'staging' or 'prod'")
        env = "prod"
    elif grant is not None and env not in grant.environments:
        refusals.append(f"{ctx.agent.id} is not granted {operation} in {env}")
        ev.append(_ev("grant", f"{operation} not granted in {env}", "state"))
    elif grant is not None:
        ev.append(_ev("grant", f"{ctx.agent.id} may {operation} in {env}", "state"))
    if not env_given:
        ev.append(_ev("environment", "not specified; assuming prod", "policy"))

    # completeness
    for k in spec["required"]:
        v = params.get(k)
        if k not in params or v is None or v == "":
            missing.append(f"{k} is required")

    r = RESOLVERS[operation](params, ctx.facts, spec)
    ev.extend(r.evidence)
    missing.extend(r.missing)
    refusals.extend(r.refusals)
    qty = r.quantity

    # limits
    lim = spec["limits"]
    win = spec.get("window", {})
    if qty is not None:
        if grant is not None and grant.max_quantity is not None and qty > grant.max_quantity:
            refusals.append(f"{qty} exceeds this agent's cap of {grant.max_quantity}")
            ev.append(_ev("agent_cap", f"{qty} > {grant.max_quantity}", "state"))
        if qty > lim["hard"]:
            refusals.append(f"{qty} exceeds the hard limit of {lim['hard']} per action")
            ev.append(_ev("per_action_hard_limit", f"{qty} > {lim['hard']}", "policy"))
        elif qty > lim["auto"]:
            escalations.append(f"{qty} exceeds the automatic limit of {lim['auto']} per action")
            ev.append(_ev("per_action_auto_limit", f"{qty} > {lim['auto']}", "policy"))
        else:
            ev.append(_ev("per_action_auto_limit", f"{qty} <= {lim['auto']}", "policy"))
        hours = win.get("hours", 24)
        total_agent = ctx.usage_agent + qty
        total_global = ctx.usage_global + qty
        if "agent_hard" in win and total_agent > win["agent_hard"]:
            refusals.append(f"{total_agent} in {hours}h would exceed this agent's hard cap of {win['agent_hard']}")
            ev.append(_ev("window_agent_hard", f"{ctx.usage_agent} already + {qty} = {total_agent} > {win['agent_hard']}", "policy"))
        elif "agent_auto" in win and total_agent > win["agent_auto"]:
            escalations.append(
                f"{total_agent} in {hours}h exceeds the automatic window limit of {win['agent_auto']} (cumulative, not per action)"
            )
            ev.append(_ev("window_agent_auto", f"{ctx.usage_agent} already + {qty} = {total_agent} > {win['agent_auto']}", "policy"))
        if "global_hard" in win and total_global > win["global_hard"]:
            refusals.append(f"{total_global} across all agents in {hours}h would exceed {win['global_hard']}")
            ev.append(_ev("window_global_hard", f"{ctx.usage_global} already + {qty} = {total_global} > {win['global_hard']}", "policy"))

    if r.reversibility == "irreversible":
        escalations.append("irreversible actions need a human")

    # change window
    cw = spec.get("change_window")
    if cw and env in cw["environments"]:
        h = ctx.now.astimezone(UTC).hour
        if not (cw["start_hour"] <= h < cw["end_hour"]):
            nxt = _next_window_start(ctx.now, cw["start_hour"])
            retry_after = iso(nxt)
            defers.append(f"{env} changes are only allowed {cw['start_hour']:02d}:00-{cw['end_hour']:02d}:00 UTC")
            ev.append(_ev("change_window", f"now {h:02d}:xx UTC is outside {cw['start_hour']:02d}-{cw['end_hour']:02d}", "policy"))

    q = qty or 0
    risk = risk_score(spec, r.reversibility, q, env, ctx.usage_agent, r.inconsistent)
    if risk >= ctx.policy["escalate_risk"]:
        escalations.append(f"risk score {risk} is at or above {ctx.policy['escalate_risk']}")

    if refusals:
        outcome, reasons = "refuse", refusals
    elif missing:
        outcome, reasons = "ask", missing
    elif defers:
        outcome, reasons = "defer", defers
    elif escalations:
        outcome, reasons = "escalate", escalations
    else:
        outcome, reasons = "execute", ["within every limit"]

    if ctx.mode == "execute" and outcome == "escalate" and ctx.human_approved:
        outcome, reasons = "execute", ["human approval covers: " + "; ".join(escalations)]
        ev.append(_ev("human_approval", "a recorded approval matches this exact action", "state"))

    conf = 100
    if r.inconsistent:
        conf -= 20
    if not env_given:
        conf -= 5
    if outcome == "execute" and qty is not None and lim["auto"] > 0 and qty * 100 >= 80 * lim["auto"]:
        conf -= 10
        ev.append(_ev("near_limit", f"{qty} is within 20% of the automatic limit {lim['auto']}", "policy"))
    conf = max(30, conf)

    return Decision(
        outcome, conf, risk, r.reversibility, qty, env, ev, missing if outcome == "ask" else [],
        reasons, retry_after if outcome == "defer" else None, ctx.policy_version,
    )


def context_to_dict(ctx: Context) -> dict[str, Any]:
    """Everything the decision depended on except the policy body (stored by version)."""
    return {
        "now": iso(ctx.now),
        "agent": {
            "id": ctx.agent.id, "disabled": ctx.agent.disabled,
            "grants": [
                {"operation": g.operation, "environments": list(g.environments), "max_quantity": g.max_quantity}
                for g in ctx.agent.grants
            ],
        },
        "facts": ctx.facts, "usage_agent": ctx.usage_agent, "usage_global": ctx.usage_global,
        "kill_switch": ctx.kill_switch, "kill_reason": ctx.kill_reason,
        "mode": ctx.mode, "human_approved": ctx.human_approved,
    }


def context_from_dict(d: dict[str, Any], policy: dict[str, Any], policy_version: str) -> Context:
    from .canonical import parse_iso
    a = d["agent"]
    return Context(
        now=parse_iso(d["now"]),
        agent=AgentInfo(a["id"], a["disabled"], tuple(grant_from_dict(g) for g in a["grants"])),
        policy=policy, policy_version=policy_version, facts=d["facts"],
        usage_agent=d["usage_agent"], usage_global=d["usage_global"],
        kill_switch=d["kill_switch"], kill_reason=d.get("kill_reason"),
        mode=d["mode"], human_approved=d["human_approved"],
    )


def replay(operation: str, params: dict[str, Any], stored_ctx: dict[str, Any], policy: dict[str, Any],
           policy_version: str) -> Decision:
    """Re-run a past decision from its stored inputs. Used by tests and `verify`."""
    return evaluate(operation, params, context_from_dict(stored_ctx, policy, policy_version))
