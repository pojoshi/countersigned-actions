"""Behaviour against a real Postgres: the invariants in docs/INVARIANTS.md, one test each."""

import json
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from countersigned import ActionProposal
from countersigned.errors import Conflict, Expired, Forbidden, NotFound
from countersigned.proposers import submit
from countersigned.seed import default_policy
from conftest import DSN, delete_params, op

DELETE = "customer_data.delete_records"
REFUND = "payments.refund"
SCALE = "infrastructure.scale_down"


def refund(amount=4000, payment="PAY-3", ticket="TKT-1004", **kw):
    return op(REFUND, payment_id=payment, amount_cents=amount, ticket_id=ticket, reason="customer complaint", **kw)


def delete(**kw):
    return op(DELETE, **delete_params(**kw))


def live(gate, segment):
    with gate.db.transaction() as c:
        return c.execute("SELECT count(*) AS n FROM sim_customers WHERE segment=%s AND deleted_at IS NULL", (segment,)).fetchone()["n"]


def refunded(gate, pid="PAY-3"):
    with gate.db.transaction() as c:
        return c.execute("SELECT refunded_cents FROM sim_payments WHERE id=%s", (pid,)).fetchone()["refunded_cents"]


def count(gate, sql, *a):
    with gate.db.transaction() as c:
        return c.execute(sql, a).fetchone()["n"]


def events(gate, kind=None):
    return [e for e in gate.audit_page(0, 10000) if kind is None or e["event_type"] == kind]


# ---------------------------------------------------------------- happy path

def test_allowed_action_is_decided_recorded_then_executed(gate):
    a = submit(gate, "support-bot", delete())
    assert a["status"] == "queued" and a["decision"]["outcome"] == "execute"
    assert live(gate, "eu-erasure-single") == 3  # nothing happens until the worker runs
    out = gate.run_worker_until_empty()
    assert [o["result"] for o in out] == ["executed"]
    assert live(gate, "eu-erasure-single") == 0
    assert gate.get_action(a["id"])["status"] == "executed"
    kinds = [e["event_type"] for e in events(gate) if e["action_id"] == a["id"]]
    assert kinds == ["decision", "executed"]
    assert gate.verify_audit()["ok"]


def test_decision_and_audit_entry_are_one_transaction(gate, monkeypatch):
    import countersigned.service as svc

    def boom(*a, **k):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(svc.audit, "append", boom)
    with pytest.raises(RuntimeError):
        gate.propose("support-bot", ActionProposal(**delete()))
    assert count(gate, "SELECT count(*) AS n FROM actions") == 0  # no decision without its audit entry
    assert count(gate, "SELECT count(*) AS n FROM outbox") == 0


# ------------------------------------------------------------- approvals

def test_escalated_action_waits_for_a_human_and_runs_exactly_what_was_approved(gate):
    a = submit(gate, "support-bot", delete(segment="vip", count=40))
    assert a["status"] == "pending_approval"
    assert gate.run_worker_until_empty() == []
    assert live(gate, "vip") == 40
    done = gate.decide(a["id"], "alice", "approve", a["action_hash"], "verified ticket")
    assert done["status"] == "approved"
    assert [o["result"] for o in gate.run_worker_until_empty()] == ["executed"]
    assert live(gate, "vip") == 0
    assert count(gate, "SELECT count(*) AS n FROM approvals WHERE consumed_at IS NOT NULL") == 1
    assert gate.verify_audit()["ok"]


def test_approval_is_bound_to_the_exact_action(gate):
    a = submit(gate, "support-bot", delete(segment="vip", count=40))
    other = submit(gate, "support-bot", delete(segment="trial-expired", count=300))
    with pytest.raises(Conflict):
        gate.decide(a["id"], "alice", "approve", other["action_hash"])  # hash of a different action
    with pytest.raises(Conflict):
        gate.decide(a["id"], "alice", "approve", "0" * 64)
    assert gate.get_action(a["id"])["status"] == "pending_approval"


def test_agents_cannot_approve_and_one_approval_is_one_use(gate):
    a = submit(gate, "support-bot", delete(segment="vip", count=40))
    with pytest.raises(Forbidden):
        gate.decide(a["id"], "support-bot", "approve", a["action_hash"])
    gate.decide(a["id"], "alice", "approve", a["action_hash"])
    with pytest.raises(Conflict):
        gate.decide(a["id"], "bob", "approve", a["action_hash"])  # already approved: no second approval
    gate.run_worker_until_empty()
    with pytest.raises(Conflict):
        gate.decide(a["id"], "bob", "approve", a["action_hash"])  # already executed


def test_rejected_action_never_runs(gate):
    a = submit(gate, "support-bot", delete(segment="vip", count=40))
    assert gate.decide(a["id"], "alice", "reject", a["action_hash"], "not convinced")["status"] == "rejected"
    assert gate.run_worker_until_empty() == []
    assert live(gate, "vip") == 40


def test_approval_expires(gate, clock):
    a = submit(gate, "support-bot", delete(segment="vip", count=40))
    clock.advance(hours=5)  # ttl is 240 minutes
    with pytest.raises(Expired):
        gate.decide(a["id"], "alice", "approve", a["action_hash"])
    assert gate.get_action(a["id"])["status"] == "expired"
    assert gate.run_worker_until_empty() == []


def test_sweep_expires_stale_pending_actions(gate, clock):
    submit(gate, "support-bot", delete(segment="vip", count=40))
    clock.advance(hours=5)
    assert gate.expire_stale() == 1


# ----------------------------------------------------- re-check at execution

def test_kill_switch_stops_queued_work(gate):
    a = submit(gate, "support-bot", delete())
    gate.set_kill_switch(True, "admin", "incident 42")
    out = gate.run_worker_until_empty()
    assert out[0]["result"] == "blocked" and "kill switch" in out[0]["why"]
    assert live(gate, "eu-erasure-single") == 3
    assert gate.get_action(a["id"])["status"] == "blocked"
    # new proposals are refused while it is on, and allowed again after
    assert submit(gate, "support-bot", delete(segment="eu-erasure-batch", count=20))["status"] == "refused"
    gate.set_kill_switch(False, "admin")
    assert submit(gate, "support-bot", delete(segment="eu-erasure-batch", count=20))["status"] == "queued"


def test_kill_switch_beats_an_approval(gate):
    a = submit(gate, "support-bot", delete(segment="vip", count=40))
    gate.decide(a["id"], "alice", "approve", a["action_hash"])
    gate.set_kill_switch(True, "admin", "stop")
    assert gate.run_worker_until_empty()[0]["result"] == "blocked"
    assert live(gate, "vip") == 40


def test_disabled_agent_and_revoked_grant_block_already_queued_actions(gate):
    submit(gate, "support-bot", delete())
    gate.set_agent_disabled("support-bot", True, "admin")
    assert gate.run_worker_until_empty()[0]["result"] == "blocked"
    gate.set_agent_disabled("support-bot", False, "admin")
    b = submit(gate, "support-bot", delete(segment="eu-erasure-batch", count=20))
    with gate.db.transaction() as c:
        c.execute("UPDATE principals SET grants = '[]'::jsonb WHERE id = 'support-bot'")
    out = gate.run_worker_until_empty()
    assert out[0]["result"] == "blocked" and "no grant" in out[0]["why"]
    assert live(gate, "eu-erasure-batch") == 20 and gate.get_action(b["id"])["status"] == "blocked"


def test_data_that_changed_after_approval_blocks_the_execution(gate):
    a = submit(gate, "support-bot", delete(segment="vip", count=40))
    gate.decide(a["id"], "alice", "approve", a["action_hash"])
    with gate.db.transaction() as c:  # 5 more VIPs appear after the human looked
        c.execute("INSERT INTO sim_customers (segment, email) SELECT 'vip', 'late'||g FROM generate_series(1,5) g")
    out = gate.run_worker_until_empty()
    assert out[0]["result"] == "blocked" and "45" in out[0]["why"]
    assert live(gate, "vip") == 45


def test_effect_guard_catches_a_race_the_policy_check_missed(gate, monkeypatch):
    """Simulate the facts being read a moment too early: the UPDATE itself still has to match."""
    a = submit(gate, "support-bot", delete(segment="vip", count=40))
    gate.decide(a["id"], "alice", "approve", a["action_hash"])
    real = gate._facts
    stale = real  # facts that claim 40, while the table now holds 45

    def lying_facts(conn, operation, p):
        f = stale(conn, operation, p)
        if operation == DELETE:
            f["segment_sizes"] = {p["segment"]: 40}
        return f

    with gate.db.transaction() as c:
        c.execute("INSERT INTO sim_customers (segment, email) SELECT 'vip', 'late'||g FROM generate_series(1,5) g")
    monkeypatch.setattr(gate, "_facts", lying_facts)
    out = gate.run_worker_until_empty()
    assert out[0]["result"] == "blocked" and "45 rows matched" in out[0]["why"]
    assert live(gate, "vip") == 45  # rolled back: nothing was deleted
    assert count(gate, "SELECT count(*) AS n FROM executions") == 0


def test_policy_tightened_after_approval_applies_at_execution(gate):
    a = submit(gate, "support-bot", delete())
    v1 = a["policy_version"]
    tighter = default_policy()
    tighter["operations"][DELETE]["limits"]["auto"] = 1
    v2 = gate.activate_policy(tighter, actor="admin")
    assert v1 != v2
    out = gate.run_worker_until_empty()
    assert out[0]["result"] == "blocked"
    assert gate.get_action(a["id"])["policy_version"] == v1  # the decision keeps the version it was made under
    b = submit(gate, "support-bot", delete(segment="eu-erasure-batch", count=20))
    assert b["policy_version"] == v2 and b["status"] == "pending_approval"


# ------------------------------------------------- idempotency and duplicates

def test_idempotency_key_returns_the_same_action(gate):
    p = delete() | {"idempotency_key": "k-1"}
    a = submit(gate, "support-bot", p)
    b = submit(gate, "support-bot", p)
    assert b["id"] == a["id"] and b["duplicate"] is True
    assert count(gate, "SELECT count(*) AS n FROM actions") == 1
    with pytest.raises(Conflict):
        gate.propose("support-bot", ActionProposal(**(delete(segment="vip", count=40) | {"idempotency_key": "k-1"})))


def test_duplicate_delivery_executes_once(gate):
    a = submit(gate, "support-bot", delete())
    with gate.db.transaction() as c:
        c.execute("INSERT INTO outbox (action_id, topic, created_at) VALUES (%s, 'execute', now())", (a["id"],))
    out = gate.run_worker_until_empty()
    assert [o["result"] for o in out] == ["executed", "ignored"]
    assert count(gate, "SELECT count(*) AS n FROM executions") == 1
    assert live(gate, "eu-erasure-single") == 0


def test_database_refuses_an_execution_nobody_permitted(gate):
    refused = submit(gate, "support-bot", delete(segment="vip", count=3000))
    assert refused["status"] == "refused"
    pending = submit(gate, "support-bot", delete(segment="vip", count=40))
    for aid in (refused["id"], pending["id"]):
        with pytest.raises(psycopg.errors.CheckViolation), gate.db.transaction() as c:
            c.execute("INSERT INTO executions (action_id, started_at, result) VALUES (%s, now(), '{}'::jsonb)", (aid,))
    # and a real execution cannot happen twice
    ok = submit(gate, "support-bot", delete())
    gate.run_worker_until_empty()
    with pytest.raises(psycopg.errors.UniqueViolation), gate.db.transaction() as c:
        c.execute("INSERT INTO executions (action_id, started_at, result) VALUES (%s, now(), '{}'::jsonb)", (ok["id"],))


# ---------------------------------------------------------------- windows

def test_splitting_a_large_job_into_small_pieces_is_caught(gate):
    """60 refunds of 4000 (each under the 5000 per-action limit) against a 20000 / 24h window."""
    statuses = [submit(gate, "finance-bot", refund(4000))["status"] for _ in range(60)]
    assert statuses.count("queued") == 5                      # 5 x 4000 = 20000: the automatic window
    assert statuses.count("pending_approval") == 45           # the rest need a human, and still count
    assert statuses.count("refused") == 10                    # 50 x 4000 = 200000: the hard cap
    gate.run_worker_until_empty()
    assert refunded(gate) == 20000                            # only the allowed slice moved money


def test_window_slides(gate, clock):
    for _ in range(5):
        submit(gate, "finance-bot", refund(4000))
    assert submit(gate, "finance-bot", refund(4000))["status"] == "pending_approval"
    clock.advance(hours=25)
    assert submit(gate, "finance-bot", refund(4000))["status"] == "queued"


def test_earlier_actions_are_not_blocked_by_later_arrivals(gate):
    for _ in range(5):
        submit(gate, "finance-bot", refund(4000))
    submit(gate, "finance-bot", refund(4000))  # 6th: pending, counts toward the window
    out = gate.run_worker_until_empty()
    assert [o["result"] for o in out] == ["executed"] * 5
    assert refunded(gate) == 20000


def test_concurrent_proposals_cannot_overshoot_a_window(gate):
    def one(_):
        return submit(gate, "finance-bot", refund(4000))["status"]

    with ThreadPoolExecutor(max_workers=12) as ex:
        statuses = list(ex.map(one, range(12)))
    assert statuses.count("queued") == 5 and statuses.count("pending_approval") == 7


def test_many_workers_execute_each_action_exactly_once(gate):
    for _ in range(20):
        submit(gate, "finance-bot", refund(1000))
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = [r for part in ex.map(lambda _: gate.run_worker_until_empty(), range(4)) for r in part]
    assert sorted(r["result"] for r in results) == ["executed"] * 20
    assert count(gate, "SELECT count(*) AS n FROM executions") == 20
    assert refunded(gate) == 20000
    assert gate.verify_audit()["ok"]


def test_kill_switch_races_with_workers_and_nothing_slips_through(gate):
    for _ in range(20):
        submit(gate, "finance-bot", refund(1000))
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(gate.run_worker_until_empty) for _ in range(4)]
        gate.set_kill_switch(True, "admin", "race")
        for f in futs:
            f.result()
    kill_seq = next(e["seq"] for e in events(gate, "kill_switch_on"))
    for e in events(gate, "executed"):
        assert e["seq"] < kill_seq  # every execution was recorded before the switch flipped
    gate.run_worker_until_empty()
    assert count(gate, "SELECT count(*) AS n FROM executions") == len(events(gate, "executed"))


# ---------------------------------------------------------------- deferral

def test_deferred_action_is_redecided_when_its_window_opens(gate, clock):
    a = submit(gate, "infra-bot", op(SCALE, service="web", replicas_from=10, replicas_to=9, reason="cost", environment="prod"))
    assert a["status"] == "deferred" and a["decision"]["retry_after"] == "2026-10-27T01:00:00.000Z"
    assert gate.release_due() == 0
    clock.set(clock.t.replace(day=27, hour=1, minute=5))
    assert gate.release_due() == 1
    assert gate.run_worker_until_empty()[0]["result"] == "executed"
    with gate.db.transaction() as c:
        assert c.execute("SELECT replicas FROM sim_services WHERE name='web'").fetchone()["replicas"] == 9
    assert [e["event_type"] for e in events(gate) if e["action_id"] == a["id"]] == ["decision", "redecision", "executed"]


# ----------------------------------------------------------- trust boundary

def test_malformed_model_output_never_becomes_an_action(gate):
    for raw in (
        {"operation": DELETE, "params": delete_params(), "approved": True},
        '{"operation": "payments.refund", "params": {"amount_cents": 1.5}}',
        "not json at all",
        '{"operation": "payments.refund", "params": {"nested": {"a": 1}}}',
        [1, 2, 3],
        {"operation": DELETE, "params": {"reason": {"leak": "SECRET-VALUE-XYZ"}}},
    ):
        assert submit(gate, "support-bot", raw)["status"] == "malformed"
    assert count(gate, "SELECT count(*) AS n FROM actions") == 0
    assert len(events(gate, "malformed_proposal")) == 6
    assert "SECRET-VALUE-XYZ" not in json.dumps(events(gate, "malformed_proposal"))  # a hash and an error, never the content


def test_unknown_agent_is_rejected(gate):
    with pytest.raises(NotFound):
        gate.propose("ghost", ActionProposal(**delete()))


# ----------------------------------------------------------------- audit

def test_every_decision_can_be_replayed_from_the_log(gate):
    ids = [
        submit(gate, "support-bot", delete())["id"],
        submit(gate, "support-bot", delete(segment="vip", count=40))["id"],
        submit(gate, "support-bot", delete(segment="vip", count=30))["id"],   # mismatch: ask
        submit(gate, "support-bot", delete(segment="vip", count=3000))["id"],  # refuse
        submit(gate, "infra-bot", op(SCALE, service="web", replicas_from=10, replicas_to=9, reason="x"))["id"],
    ]
    for i in ids:
        r = gate.replay_decision(i)
        assert r["matches"], r


def test_audit_log_cannot_be_edited_with_ordinary_access(gate):
    submit(gate, "support-bot", delete())
    for sql in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), gate.db.transaction() as c:
            c.execute(sql)


def test_tampering_by_someone_who_bypasses_the_triggers_is_detected(gate):
    for _ in range(3):
        submit(gate, "finance-bot", refund(1000))
    gate.run_worker_until_empty()
    assert gate.verify_audit()["ok"]
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
        c.execute("UPDATE audit_log SET payload = jsonb_set(payload, '{decision,risk_score}', '1') WHERE seq = 2")
        c.execute("ALTER TABLE audit_log ENABLE TRIGGER USER")
    bad = gate.verify_audit()
    assert not bad["ok"] and bad["first_bad_seq"] == 2 and "edited" in bad["reason"]


def test_deleted_or_reordered_entries_are_detected(gate):
    for _ in range(3):
        submit(gate, "finance-bot", refund(1000))
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
        c.execute("DELETE FROM audit_log WHERE seq = 2")
        c.execute("ALTER TABLE audit_log ENABLE TRIGGER USER")
    bad = gate.verify_audit()
    assert not bad["ok"] and bad["first_bad_seq"] == 3
