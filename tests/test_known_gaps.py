"""Cases that get through today. They are tests so the limits in docs/FAILURE_TESTS.md are executable
claims, not prose: if one of these starts failing, the gate got stricter and the doc is out of date."""

from countersigned.proposers import submit
from conftest import delete_params, op

REFUND = "payments.refund"


def refund(amount=4000, ticket="TKT-1004"):
    return op(REFUND, payment_id="PAY-3", amount_cents=amount, ticket_id=ticket, reason="customer complaint")


def refunded(gate):
    with gate.db.transaction() as c:
        return c.execute("SELECT refunded_cents FROM sim_payments WHERE id='PAY-3'").fetchone()["refunded_cents"]


def test_gap_slow_roll_under_the_daily_window(gate, clock):
    """5 refunds of 40.00 a day is exactly the allowed rate. Doing it for 10 days moves 2,000.00 with no human involved,
    because the only cumulative limit is 24 hours long."""
    for _ in range(10):
        for _ in range(5):
            assert submit(gate, "finance-bot", refund())["status"] == "queued"
        clock.advance(hours=25)
    gate.run_worker_until_empty()
    assert refunded(gate) == 200000
    assert gate.list_actions(status="pending_approval") == []


def test_gap_two_agents_each_under_their_own_window(gate):
    """Windows are per agent; only the (much larger) global cap bounds the combination."""
    ran = 0
    for agent in ("finance-bot", "support-bot"):
        for _ in range(5):
            ran += submit(gate, agent, refund())["status"] == "queued"
    assert ran == 10
    gate.run_worker_until_empty()
    assert refunded(gate) == 40000  # twice what one agent is allowed to move unattended


def test_gap_one_real_ticket_can_cover_unrelated_work(gate):
    """Tickets are checked for existence, status and kind, not for what they authorise."""
    a = submit(gate, "support-bot", op("customer_data.delete_records", **delete_params("eu-erasure-batch", 20, "TKT-1002")))
    b = submit(gate, "support-bot", op("customer_data.delete_records", **delete_params("eu-erasure-b2", 20, "TKT-1002")))
    assert a["status"] == "queued" and b["status"] == "queued"
