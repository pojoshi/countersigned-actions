"""Effects: the only code that changes the simulated business.

Each effect runs inside the executor's transaction and verifies, with the row counts
the database reports, that the world still matches what was decided. If it does not
it raises EffectBlocked and the whole execution rolls back. That is the last line of
defence against check-then-act races: the policy check read the facts a moment ago;
the UPDATE itself must still find exactly what was approved.
"""

from datetime import datetime
from typing import Any
from collections.abc import Callable

from .errors import EffectBlocked


def _delete_records(conn: Any, p: dict[str, Any], action_id: str, now: datetime) -> dict[str, Any]:
    if p.get("hard") is True:
        n = conn.execute(
            "DELETE FROM sim_customers WHERE segment = %s AND deleted_at IS NULL", (p["segment"],)
        ).rowcount
        mode = "hard"
    else:
        n = conn.execute(
            "UPDATE sim_customers SET deleted_at = %s WHERE segment = %s AND deleted_at IS NULL", (now, p["segment"])
        ).rowcount
        mode = "soft"
    if n != p["count"]:
        raise EffectBlocked(f"segment '{p['segment']}' changed: {n} rows matched, {p['count']} were approved")
    return {"deleted": n, "mode": mode, "segment": p["segment"]}


def _export_records(conn: Any, p: dict[str, Any], action_id: str, now: datetime) -> dict[str, Any]:
    conn.execute(
        "INSERT INTO sim_exports (action_id, destination, row_count) VALUES (%s, %s, %s)",
        (action_id, p["destination"], p["count"]),
    )
    return {"exported": p["count"], "destination": p["destination"]}


def _refund(conn: Any, p: dict[str, Any], action_id: str, now: datetime) -> dict[str, Any]:
    n = conn.execute(
        "UPDATE sim_payments SET refunded_cents = refunded_cents + %s "
        "WHERE id = %s AND amount_cents - refunded_cents >= %s",
        (p["amount_cents"], p["payment_id"], p["amount_cents"]),
    ).rowcount
    if n != 1:
        raise EffectBlocked(f"payment {p['payment_id']} no longer has {p['amount_cents']} cents refundable")
    return {"refunded_cents": p["amount_cents"], "payment_id": p["payment_id"]}


def _transfer(conn: Any, p: dict[str, Any], action_id: str, now: datetime) -> dict[str, Any]:
    conn.execute(
        "INSERT INTO sim_transfers (action_id, to_account, amount_cents) VALUES (%s, %s, %s)",
        (action_id, p["to_account"], p["amount_cents"]),
    )
    return {"transferred_cents": p["amount_cents"], "to_account": p["to_account"]}


def _scale_down(conn: Any, p: dict[str, Any], action_id: str, now: datetime) -> dict[str, Any]:
    n = conn.execute(
        "UPDATE sim_services SET replicas = %s WHERE name = %s AND replicas = %s",
        (p["replicas_to"], p["service"], p["replicas_from"]),
    ).rowcount
    if n != 1:
        raise EffectBlocked(f"service {p['service']} no longer runs {p['replicas_from']} replicas")
    return {"service": p["service"], "replicas": p["replicas_to"]}


EFFECTS: dict[str, Callable[[Any, dict[str, Any], str, datetime], dict[str, Any]]] = {
    "customer_data.delete_records": _delete_records,
    "customer_data.export_records": _export_records,
    "payments.refund": _refund,
    "payments.transfer": _transfer,
    "infrastructure.scale_down": _scale_down,
}


def apply(conn: Any, operation: str, params: dict[str, Any], action_id: str, now: datetime) -> dict[str, Any]:
    return EFFECTS[operation](conn, params, action_id, now)
