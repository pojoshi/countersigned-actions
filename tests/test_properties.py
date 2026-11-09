"""Property-based tests. These need no database: the engine and the hash chain are pure."""

import json
from datetime import datetime, UTC

from hypothesis import HealthCheck, given, settings, strategies as st

from countersigned.audit import chain_hash, verify_rows
from countersigned.canonical import GENESIS, canonical_json
from countersigned.engine import (
    AgentInfo, Context, Grant, RESOLVERS, context_from_dict, context_to_dict, evaluate,
)
from countersigned.models import ProposalRejected, parse_proposal
from countersigned.seed import default_policy
from countersigned.service import policy_version_of

POLICY = default_policy()
VERSION = policy_version_of(POLICY)
NOON = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
OPS = sorted(RESOLVERS)
ALL_GRANTS = tuple(Grant(o, ("staging", "prod")) for o in OPS)

FACTS = {
    "tickets": {t: {"kind": k, "status": "open"} for t, k in
                (("T-E", "erasure_request"), ("T-D", "data_request"), ("T-R", "refund_request"))},
    "segment_sizes": {"s": 7},
    "payments": {"P": {"amount_cents": 10**9, "refunded_cents": 0}},
    "services": {"web": 10},
}

scalars = st.one_of(st.booleans(), st.integers(-10**6, 10**6), st.text(max_size=12))
known_keys = st.sampled_from([
    "segment", "count", "ticket_id", "reason", "hard", "destination", "payment_id", "amount_cents",
    "to_account", "service", "replicas_from", "replicas_to", "environment",
])
params_st = st.dictionaries(known_keys | st.text(max_size=8), scalars, max_size=10)


def ctx(agent=None, **kw):
    agent = agent or AgentInfo("a", False, ALL_GRANTS)
    return Context(now=NOON, agent=agent, policy=POLICY, policy_version=VERSION, facts=FACTS, **kw)


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(op=st.sampled_from([*OPS, "x.y", ""]), params=params_st)
def test_engine_never_crashes_on_weird_model_output(op, params):
    d = evaluate(op, params, ctx())
    assert d.outcome in ("execute", "ask", "defer", "escalate", "refuse")
    assert 0 <= d.risk_score <= 100 and 30 <= d.confidence <= 100
    assert d.reversibility in ("reversible", "partial", "irreversible")
    json.dumps(d.to_dict())  # always serialisable


@settings(max_examples=300)
@given(op=st.sampled_from(OPS), params=params_st, granted=st.sets(st.sampled_from(OPS)))
def test_nothing_executes_without_a_grant(op, params, granted):
    agent = AgentInfo("a", False, tuple(Grant(o, ("staging", "prod")) for o in granted))
    d = evaluate(op, params, ctx(agent))
    if op not in granted:
        assert d.outcome == "refuse"


@settings(max_examples=300)
@given(op=st.sampled_from([*OPS, "x.y"]), params=params_st, mode=st.sampled_from(["propose", "execute"]), approved=st.booleans())
def test_kill_switch_overrides_everything_including_approvals(op, params, mode, approved):
    assert evaluate(op, params, ctx(kill_switch=True, mode=mode, human_approved=approved)).outcome == "refuse"


@settings(max_examples=300)
@given(op=st.sampled_from(OPS), params=params_st, ua=st.integers(0, 600), ug=st.integers(0, 1500))
def test_decisions_are_deterministic_and_replayable(op, params, ua, ug):
    c = ctx(usage_agent=ua, usage_global=ug)
    first = evaluate(op, params, c)
    assert evaluate(op, params, c).to_dict() == first.to_dict()
    stored = json.loads(json.dumps(context_to_dict(c)))  # what the audit log would hold
    assert evaluate(op, params, context_from_dict(stored, POLICY, VERSION)).to_dict() == first.to_dict()


ORDER = {"execute": 0, "escalate": 1, "refuse": 2}


@settings(max_examples=300)
@given(a=st.integers(1, 700000), b=st.integers(1, 700000))
def test_a_bigger_refund_is_never_treated_more_leniently(a, b):
    lo, hi = sorted((a, b))
    p = {"payment_id": "P", "ticket_id": "T-R", "reason": "x"}
    d_lo = evaluate("payments.refund", p | {"amount_cents": lo}, ctx())
    d_hi = evaluate("payments.refund", p | {"amount_cents": hi}, ctx())
    assert ORDER[d_lo.outcome] <= ORDER[d_hi.outcome]
    assert d_lo.risk_score <= d_hi.risk_score


@settings(max_examples=200)
@given(pieces=st.lists(st.integers(1, 5000), min_size=1, max_size=120))
def test_splitting_never_gets_more_through_than_the_window_allows(pieces):
    """Feed a stream of small refunds through the engine, counting what it lets through,
    exactly as the service does (executed, queued and pending all count)."""
    win = POLICY["operations"]["payments.refund"]["window"]
    counted = auto = 0
    for amt in pieces:
        d = evaluate(
            "payments.refund",
            {"payment_id": "P", "ticket_id": "T-R", "reason": "x", "amount_cents": amt},
            ctx(usage_agent=counted, usage_global=counted),
        )
        if d.outcome in ("execute", "escalate"):
            counted += amt
        if d.outcome == "execute":
            auto += amt
    assert auto <= win["agent_auto"]
    assert counted <= win["agent_hard"]


# ------------------------------------------------------------ hash chain

def build_chain(n):
    rows, prev = [], GENESIS
    for i in range(1, n + 1):
        row = {"seq": i, "ts": f"2026-10-26T12:00:{i % 60:02d}.000Z", "event_type": "decision",
               "action_id": f"act_{i}", "actor": "bot", "payload": {"n": i, "text": "é✓"}, "prev_hash": prev}
        row["hash"] = chain_hash(prev, i, row["ts"], row["event_type"], row["action_id"], row["actor"], row["payload"])
        rows.append(row)
        prev = row["hash"]
    return rows


@given(n=st.integers(0, 30))
def test_an_untouched_chain_verifies(n):
    assert verify_rows(build_chain(n)).ok


@settings(max_examples=200)
@given(n=st.integers(2, 25), data=st.data())
def test_any_edit_deletion_or_reorder_is_detected(n, data):
    rows = [dict(r) for r in build_chain(n)]
    i = data.draw(st.integers(0, n - 1))
    kind = data.draw(st.sampled_from(["payload", "actor", "ts", "event_type", "delete", "swap", "hash"]))
    if kind == "delete":
        del rows[i]
    elif kind == "swap":
        j = data.draw(st.integers(0, n - 1).filter(lambda x: x != i))
        rows[i], rows[j] = rows[j], rows[i]
    elif kind == "payload":
        rows[i]["payload"] = {"n": -1}
    elif kind == "hash":
        rows[i]["hash"] = "f" * 64
    else:
        rows[i][kind] = rows[i][kind] + "x"
    res = verify_rows(rows)
    if kind == "delete" and i == n - 1:
        # dropping the *last* entry leaves a valid shorter chain: only an external copy of the head can catch it
        assert res.ok and res.entries == n - 1
    else:
        assert not res.ok


@given(st.dictionaries(st.text(max_size=5), st.one_of(st.integers(-10**9, 10**9), st.text(max_size=5), st.booleans(), st.none()), max_size=8))
def test_canonical_json_ignores_key_order(d):
    shuffled = dict(reversed(list(d.items())))
    assert canonical_json(d) == canonical_json(shuffled)


# ---------------------------------------------------------- trust boundary

@settings(max_examples=400)
@given(st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text(max_size=20)),
    lambda c: st.one_of(st.lists(c, max_size=4), st.dictionaries(st.text(max_size=8), c, max_size=4)),
    max_leaves=12,
))
def test_parse_proposal_only_ever_raises_proposal_rejected(raw):
    try:
        p = parse_proposal(raw)
    except ProposalRejected:
        return
    assert isinstance(p.params, dict)
    assert all(isinstance(v, (bool, int, str)) for v in p.params.values())


def test_model_output_cannot_smuggle_approval_or_identity():
    for extra in ("approved", "approval_id", "agent_id", "human_approved", "status"):
        try:
            parse_proposal({"operation": "payments.refund", "params": {}, extra: True})
        except ProposalRejected:
            continue
        raise AssertionError(f"{extra} was accepted")
