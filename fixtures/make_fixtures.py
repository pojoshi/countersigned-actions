"""Regenerate fixtures/conformance.json (engine + hash vectors, shared with the TypeScript playground).

    python fixtures/make_fixtures.py

The inputs below are written by hand; the expected outputs come from the Python engine and
were reviewed by eye when first generated. tests/test_conformance.py fails if the engine
drifts from the file, and web/test/conformance.test.ts fails if the TypeScript port does,
so a behaviour change has to be made on purpose, in both places.
"""

import json
from datetime import datetime, UTC
from pathlib import Path

from countersigned.audit import chain_hash
from countersigned.canonical import GENESIS, action_hash, canonical_json, sha256_hex
from countersigned.engine import AgentInfo, Context, Grant, context_to_dict, evaluate
from countersigned.seed import AGENTS, default_policy
from countersigned.service import policy_version_of

POLICY = default_policy()
VERSION = policy_version_of(POLICY)
NOON = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
THREE = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)

FACTS = {
    "tickets": {
        "TKT-1": {"kind": "erasure_request", "status": "open"},
        "TKT-D": {"kind": "data_request", "status": "open"},
        "TKT-R": {"kind": "refund_request", "status": "open"},
        "TKT-C": {"kind": "refund_request", "status": "closed"},
    },
    "segment_sizes": {"small": 3, "twenty": 20, "mid": 40, "big": 600, "huge": 5000, "empty": 0},
    "payments": {
        "P1": {"amount_cents": 20000, "refunded_cents": 0},
        "P2": {"amount_cents": 9000, "refunded_cents": 8500},
    },
    "services": {"web": 10, "web-staging": 4},
}


def agent(name: str) -> AgentInfo:
    from countersigned.engine import grant_from_dict
    return AgentInfo(name, False, tuple(grant_from_dict(g) for g in AGENTS[name]))


def ctx(who="support-bot", now=NOON, **kw) -> Context:
    a = kw.pop("agent", None) or agent(who)
    return Context(now=now, agent=a, policy=POLICY, policy_version=VERSION, facts=FACTS, **kw)


DEL, EXP, REF, TRF, SCALE = (
    "customer_data.delete_records", "customer_data.export_records", "payments.refund",
    "payments.transfer", "infrastructure.scale_down",
)


def d(segment="small", count=3, **kw):
    return DEL, {"segment": segment, "count": count, "ticket_id": "TKT-1", "reason": "erasure", **kw}


def r(amount=3000, payment="P1", ticket="TKT-R", **kw):
    return REF, {"payment_id": payment, "amount_cents": amount, "ticket_id": ticket, "reason": "complaint", **kw}


def s(frm=10, to=8, service="web", **kw):
    return SCALE, {"service": service, "replicas_from": frm, "replicas_to": to, "reason": "cost", **kw}


def e(count=50, dest="internal-warehouse", **kw):
    return EXP, {"count": count, "destination": dest, "ticket_id": "TKT-D", "reason": "report", **kw}


CASES = [
    ("small soft delete executes", d(), ctx()),
    ("delete in staging", d(environment="staging"), ctx()),
    ("delete, environment omitted", d(), ctx()),
    ("delete near the auto limit lowers confidence", d("twenty", 20, environment="prod"), ctx()),
    ("claim below data asks", d("mid", 30), ctx()),
    ("claim above data asks", d("small", 10), ctx()),
    ("claim far below data", d("big", 10), ctx()),
    ("over auto limit escalates", d("mid", 40), ctx()),
    ("over hard limit refuses", d("huge", 5000), ctx()),
    ("hard delete escalates", d(hard=True), ctx()),
    ("hard flag of wrong type asks", d(hard="yes"), ctx()),
    ("unknown segment asks", d("nowhere", 3), ctx()),
    ("empty segment, nonzero count asks", d("empty", 3), ctx()),
    ("missing ticket and reason", (DEL, {"segment": "small", "count": 3}), ctx()),
    ("invented ticket", (DEL, {"segment": "small", "count": 3, "ticket_id": "TKT-9", "reason": "x"}), ctx()),
    ("wrong ticket kind", (DEL, {"segment": "small", "count": 3, "ticket_id": "TKT-R", "reason": "x"}), ctx()),
    ("count as string", d(count="3"), ctx()),
    ("count zero", d(count=0), ctx()),
    ("count boolean", d(count=True), ctx()),
    ("window pushes over the auto limit", d(), ctx(usage_agent=48)),
    ("window at the auto limit exactly", d(), ctx(usage_agent=47)),
    ("window over the agent hard cap", d(), ctx(usage_agent=499)),
    ("window over the global cap", d(), ctx(usage_global=999)),
    ("kill switch", d(), ctx(kill_switch=True, kill_reason="incident")),
    ("disabled agent", d(), ctx(agent=AgentInfo("support-bot", True, agent("support-bot").grants))),
    ("no grant for operation", s(), ctx("support-bot")),
    ("grant limited to staging", d(environment="prod"), ctx(agent=AgentInfo("a", False, (Grant(DEL, ("staging",)),)))),
    ("unknown environment", d(environment="moon"), ctx()),
    ("agent cap", d(), ctx(agent=AgentInfo("a", False, (Grant(DEL, ("prod",), 2),)))),
    ("unknown operation", ("customer_data.drop_table", {"table": "x"}), ctx()),
    ("refund small executes", r(), ctx("finance-bot")),
    ("refund over auto escalates", r(6000), ctx("finance-bot")),
    ("refund over what is left refuses", r(1000, "P2"), ctx("finance-bot")),
    ("refund unknown payment asks", r(payment="NOPE"), ctx("finance-bot")),
    ("refund closed ticket asks", r(ticket="TKT-C"), ctx("finance-bot")),
    ("refund over agent grant cap", r(150000), ctx("finance-bot")),
    ("transfer always escalates", (TRF, {"to_account": "A1", "amount_cents": 100, "reason": "x"}), ctx("finance-bot")),
    ("transfer over hard limit", (TRF, {"to_account": "A1", "amount_cents": 2000000, "reason": "x"}), ctx("finance-bot")),
    ("export internal executes", e(), ctx("analytics-bot")),
    ("export internal over auto escalates", e(500), ctx("analytics-bot")),
    ("export external escalates", e(10, "partner-sftp"), ctx("analytics-bot")),
    ("export to unknown destination refuses", e(10, "evil.example"), ctx("analytics-bot")),
    ("scale staging executes", s(4, 3, "web-staging", environment="staging"), ctx("infra-bot")),
    ("scale prod at noon defers", s(environment="prod"), ctx("infra-bot")),
    ("scale prod at 03:00 executes", s(environment="prod"), ctx("infra-bot", now=THREE)),
    ("scale prod 4 replicas escalates at 03:00", s(10, 6, environment="prod"), ctx("infra-bot", now=THREE)),
    ("scale prod, environment omitted, noon defers", s(), ctx("infra-bot")),
    ("scale with stale replica claim asks", s(8, 7, environment="staging"), ctx("infra-bot")),
    ("scale to a higher number asks", s(10, 12, environment="staging"), ctx("infra-bot")),
    ("scale unknown service asks", s(service="ghost", environment="staging"), ctx("infra-bot")),
    ("precedence: kill switch beats defer", s(environment="prod"), ctx("infra-bot", kill_switch=True)),
    ("precedence: ask beats defer", s(environment="prod", reason=""), ctx("infra-bot")),
    ("execute mode, human approval lifts escalation", d("mid", 40), ctx(mode="execute", human_approved=True)),
    ("execute mode, no approval stays escalated", d("mid", 40), ctx(mode="execute", human_approved=False)),
    ("execute mode, approval does not lift refusal", d("huge", 5000), ctx(mode="execute", human_approved=True)),
    ("execute mode, approval does not lift a stale claim", d("mid", 30), ctx(mode="execute", human_approved=True)),
    ("unicode reason is fine", d(reason="Löschung ✓"), ctx()),
]


def main() -> None:
    decisions = []
    for name, (op, params), c in CASES:
        decisions.append({
            "name": name, "operation": op, "params": params, "context": context_to_dict(c),
            "expected": evaluate(op, params, c).to_dict(),
        })

    canon_inputs = [
        {}, {"b": 1, "a": 2}, {"z": [3, 1, 2], "a": {"y": True, "x": None}}, {"text": "é✓ \"quoted\" \\ \n"},
        {"n": -5, "big": 9007199254740991, "zero": 0}, [], "plain",
    ]
    canonical = [{"input": x, "canonical": canonical_json(x), "sha256": sha256_hex(canonical_json(x))} for x in canon_inputs]

    actions = [
        ("support-bot", DEL, d()[1]), ("finance-bot", REF, r()[1]),
        ("infra-bot", SCALE, s(environment="prod")[1]),
        ("analytics-bot", EXP, {"count": 50, "destination": "internal-warehouse", "reason": "Löschung ✓", "ticket_id": "TKT-D"}),
    ]
    action_hashes = [{"agent": a, "operation": o, "params": p, "hash": action_hash(a, o, p)} for a, o, p in actions]

    chain, prev = [], GENESIS
    for i, (ev_type, aid, actor, payload) in enumerate([
        ("decision", "act_1", "support-bot", {"status": "queued", "decision": {"outcome": "execute", "risk_score": 20}}),
        ("executed", "act_1", "worker", {"result": {"deleted": 3}}),
        ("kill_switch_on", None, "admin", {"reason": "Löschung ✓"}),
        ("decision", "act_2", "finance-bot", {"params": {"amount_cents": 4000, "payment_id": "P1"}}),
    ], start=1):
        ts = f"2026-10-26T12:00:{i:02d}.000Z"
        h = chain_hash(prev, i, ts, ev_type, aid, actor, payload)
        chain.append({"seq": i, "ts": ts, "event_type": ev_type, "action_id": aid, "actor": actor,
                      "payload": payload, "prev_hash": prev, "hash": h})
        prev = h

    out = {
        "policy_version": VERSION, "decisions": decisions, "canonical": canonical,
        "action_hashes": action_hashes, "chain": chain,
    }
    from countersigned.seed import AGENTS as A, PAYMENTS, SEGMENTS, SERVICES, TICKETS
    world = {
        "agents": A, "segments": SEGMENTS,
        "tickets": [{"id": i, "kind": k, "status": st} for i, k, st in TICKETS],
        "payments": [{"id": i, "amount_cents": a, "refunded_cents": r_} for i, a, r_ in PAYMENTS],
        "services": [{"name": n, "environment": e_, "replicas": c} for n, e_, c in SERVICES],
    }
    (Path(__file__).parent / "world.json").write_text(json.dumps(world, indent=1) + "\n")
    path = Path(__file__).parent / "conformance.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {path} ({len(decisions)} decision cases)")
    for dd in decisions:
        x = dd["expected"]
        print(f"{dd['name'][:52]:52} {x['outcome']:9} risk={x['risk_score']:3} conf={x['confidence']:3} {x['reversibility']:12} q={x['effective_quantity']}")


if __name__ == "__main__":
    main()
