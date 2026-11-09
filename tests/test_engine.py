"""The engine is pure, so most of its behaviour is tested without a database."""

from datetime import datetime, UTC


from countersigned.engine import AgentInfo, Context, Grant, evaluate
from countersigned.seed import default_policy
from countersigned.service import policy_version_of

POLICY = default_policy()
VERSION = policy_version_of(POLICY)
NOON = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
THREE = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)

SUPPORT = AgentInfo("support-bot", False, (
    Grant("customer_data.delete_records", ("staging", "prod")), Grant("payments.refund", ("prod",)),
))
INFRA = AgentInfo("infra-bot", False, (Grant("infrastructure.scale_down", ("staging", "prod")),))

FACTS = {
    "tickets": {
        "TKT-1": {"kind": "erasure_request", "status": "open"},
        "TKT-R": {"kind": "refund_request", "status": "open"},
        "TKT-C": {"kind": "refund_request", "status": "closed"},
    },
    "segment_sizes": {"small": 3, "mid": 40, "big": 600, "huge": 5000},
    "payments": {"P1": {"amount_cents": 20000, "refunded_cents": 0}, "P2": {"amount_cents": 9000, "refunded_cents": 8500}},
    "services": {"web": 10},
}


def ctx(agent=SUPPORT, now=NOON, facts=FACTS, **kw):
    return Context(now=now, agent=agent, policy=POLICY, policy_version=VERSION, facts=facts, **kw)


def delete(segment="small", count=3, **extra):
    p = {"segment": segment, "count": count, "ticket_id": "TKT-1", "reason": "erasure"}
    p.update(extra)
    return "customer_data.delete_records", p


def test_small_verified_soft_delete_executes():
    d = evaluate(*delete(), ctx())
    assert d.outcome == "execute" and d.reversibility == "reversible"
    assert d.effective_quantity == 3 and 0 <= d.risk_score < 60


def test_blast_radius_comes_from_data_not_from_the_agent():
    # Agent says 10, the segment actually holds 600: the system believes the data.
    d = evaluate(*delete("big", 10), ctx())
    assert d.effective_quantity == 600
    assert d.outcome in ("refuse", "ask")


def test_claim_mismatch_below_limits_asks_and_says_what_to_fix():
    d = evaluate(*delete("mid", 30), ctx())  # mid has 40
    assert d.outcome == "ask"
    assert any("40" in m for m in d.missing_information)
    assert d.confidence <= 80  # an inconsistent claim lowers confidence


def test_over_auto_limit_escalates_and_hard_delete_always_does():
    assert evaluate(*delete("mid", 40), ctx()).outcome == "escalate"
    d = evaluate(*delete("small", 3, hard=True), ctx())
    assert d.outcome == "escalate" and d.reversibility == "irreversible"


def test_hard_limit_refuses():
    assert evaluate(*delete("huge", 5000), ctx()).outcome == "refuse"


def test_no_grant_refuses_even_when_everything_else_is_fine():
    d = evaluate("infrastructure.scale_down", {"service": "web", "replicas_from": 10, "replicas_to": 9, "reason": "x"}, ctx())
    assert d.outcome == "refuse" and any("no grant" in r for r in d.reasons)


def test_unknown_operation_refuses():
    assert evaluate("customer_data.drop_database", {}, ctx()).outcome == "refuse"


def test_environment_must_be_granted():
    agent = AgentInfo("a", False, (Grant("customer_data.delete_records", ("staging",)),))
    assert evaluate(*delete(environment="prod"), ctx(agent)).outcome == "refuse"
    assert evaluate(*delete(environment="staging"), ctx(agent)).outcome == "execute"
    assert evaluate(*delete(environment="moon"), ctx(agent)).outcome == "refuse"


def test_missing_environment_assumes_prod_and_says_so():
    d = evaluate(*delete(), ctx())
    assert d.environment == "prod" and any(e["rule"] == "environment" for e in d.evidence)


def test_missing_fields_ask():
    d = evaluate("customer_data.delete_records", {"segment": "small", "count": 3}, ctx())
    assert d.outcome == "ask" and set(d.missing_information) == {"ticket_id is required", "reason is required"}


def test_invented_ticket_asks_instead_of_trusting_it():
    p = delete()[1] | {"ticket_id": "TKT-9999"}
    d = evaluate("customer_data.delete_records", p, ctx())
    assert d.outcome == "ask" and any("does not exist" in m for m in d.missing_information)


def test_wrong_kind_or_closed_ticket_asks():
    p = delete()[1] | {"ticket_id": "TKT-R"}
    assert evaluate("customer_data.delete_records", p, ctx()).outcome == "ask"


def test_wrong_types_ask_rather_than_crash():
    for bad in ("3", 3.5, None, True, -1, 0):
        d = evaluate("customer_data.delete_records", delete()[1] | {"count": bad}, ctx())
        assert d.outcome == "ask", bad


def test_cumulative_window_catches_splitting():
    # Each piece is within the per-action limit (25); the 24h total is what matters.
    first = evaluate(*delete("small", 3), ctx(usage_agent=0))
    assert first.outcome == "execute"
    d = evaluate(*delete("small", 3), ctx(usage_agent=48))  # 48 + 3 = 51 > 50
    assert d.outcome == "escalate" and any("cumulative" in r for r in d.reasons)


def test_window_hard_cap_and_global_cap_refuse():
    assert evaluate(*delete("small", 3), ctx(usage_agent=499)).outcome == "refuse"
    assert evaluate(*delete("small", 3), ctx(usage_global=999)).outcome == "refuse"


def test_agent_specific_cap_refuses():
    agent = AgentInfo("a", False, (Grant("customer_data.delete_records", ("prod",), max_quantity=2),))
    assert evaluate(*delete(), ctx(agent)).outcome == "refuse"


def test_kill_switch_and_disabled_agent_refuse_everything():
    assert evaluate(*delete(), ctx(kill_switch=True, kill_reason="incident")).outcome == "refuse"
    off = AgentInfo("support-bot", True, SUPPORT.grants)
    assert evaluate(*delete(), ctx(off)).outcome == "refuse"


def test_change_window_defers_with_retry_after():
    p = {"service": "web", "replicas_from": 10, "replicas_to": 9, "reason": "cost", "environment": "prod"}
    d = evaluate("infrastructure.scale_down", p, ctx(INFRA, now=NOON))
    assert d.outcome == "defer" and d.retry_after == "2026-10-27T01:00:00.000Z"
    assert evaluate("infrastructure.scale_down", p, ctx(INFRA, now=THREE)).outcome == "execute"
    stg = p | {"environment": "staging"}
    assert evaluate("infrastructure.scale_down", stg, ctx(INFRA, now=NOON)).outcome == "execute"


def test_stale_replica_claim_asks():
    p = {"service": "web", "replicas_from": 8, "replicas_to": 7, "reason": "cost", "environment": "staging"}
    d = evaluate("infrastructure.scale_down", p, ctx(INFRA))
    assert d.outcome == "ask" and any("10 replicas" in m for m in d.missing_information)


def test_refund_checks_against_the_real_payment():
    base = {"payment_id": "P1", "amount_cents": 3000, "ticket_id": "TKT-R", "reason": "damaged"}
    assert evaluate("payments.refund", base, ctx()).outcome == "execute"
    assert evaluate("payments.refund", base | {"amount_cents": 6000}, ctx()).outcome == "escalate"
    over = evaluate("payments.refund", base | {"payment_id": "P2", "amount_cents": 1000}, ctx())
    assert over.outcome == "refuse" and any("still refundable" in r for r in over.reasons)
    assert evaluate("payments.refund", base | {"payment_id": "NOPE"}, ctx()).outcome == "ask"
    assert evaluate("payments.refund", base | {"ticket_id": "TKT-C"}, ctx()).outcome == "ask"


def test_precedence_refuse_over_ask_over_defer_over_escalate():
    agent = AgentInfo("a", False, (Grant("infrastructure.scale_down", ("prod",)),))
    p = {"service": "web", "replicas_from": 10, "replicas_to": 0, "reason": "x"}  # 10 > auto(2): escalate; noon: defer
    assert evaluate("infrastructure.scale_down", p, ctx(agent, now=NOON)).outcome == "defer"
    assert evaluate("infrastructure.scale_down", p, ctx(agent, now=THREE)).outcome == "escalate"
    assert evaluate("infrastructure.scale_down", p | {"reason": ""}, ctx(agent, now=NOON)).outcome == "ask"
    assert evaluate("infrastructure.scale_down", p, ctx(agent, now=NOON, kill_switch=True)).outcome == "refuse"


def test_human_approval_only_lifts_escalation_never_refusal():
    big = delete("mid", 40)
    assert evaluate(*big, ctx(mode="execute", human_approved=False)).outcome == "escalate"
    assert evaluate(*big, ctx(mode="execute", human_approved=True)).outcome == "execute"
    assert evaluate(*big, ctx(mode="execute", human_approved=True, kill_switch=True)).outcome == "refuse"
    assert evaluate(*delete("huge", 5000), ctx(mode="execute", human_approved=True)).outcome == "refuse"
    # A claim that stopped matching the data is not covered by an approval either.
    assert evaluate(*delete("mid", 30), ctx(mode="execute", human_approved=True)).outcome == "ask"


def test_decision_has_every_required_output():
    d = evaluate(*delete(), ctx()).to_dict()
    for k in ("outcome", "confidence", "risk_score", "evidence", "missing_information", "reversibility", "policy_version"):
        assert k in d
    assert 0 <= d["confidence"] <= 100 and 0 <= d["risk_score"] <= 100 and d["evidence"]


def test_risk_score_components_are_explainable():
    # soft delete of 3, PII, prod, no window pressure: 0 + (30*3//100=0) + 10 + 10 + (20*3//500=0) = 20
    assert evaluate(*delete(), ctx()).risk_score == 20
    # hard delete adds 35 for irreversibility
    assert evaluate(*delete(hard=True), ctx()).risk_score == 55
