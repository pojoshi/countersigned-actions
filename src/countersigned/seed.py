"""Demo data and demo credentials. Local development, tests and the public sandbox only."""

import json
from importlib import resources
from typing import Any

from .service import Gate

DEMO_KEYS = {
    "support-bot": "demo-key-support-bot",
    "analytics-bot": "demo-key-analytics-bot",
    "infra-bot": "demo-key-infra-bot",
    "finance-bot": "demo-key-finance-bot",
    "alice": "demo-key-alice",
    "bob": "demo-key-bob",
    "admin": "demo-key-admin",
}

AGENTS: dict[str, list[dict[str, Any]]] = {
    "support-bot": [
        {"operation": "customer_data.delete_records", "environments": ["staging", "prod"]},
        {"operation": "payments.refund", "environments": ["prod"]},
    ],
    "analytics-bot": [{"operation": "customer_data.export_records", "environments": ["staging", "prod"]}],
    "infra-bot": [{"operation": "infrastructure.scale_down", "environments": ["staging", "prod"]}],
    "finance-bot": [
        {"operation": "payments.refund", "environments": ["prod"], "max_quantity": 100000},
        {"operation": "payments.transfer", "environments": ["prod"]},
    ],
}

SEGMENTS = {
    "inactive-2y": 600, "dormant-500": 500, "trial-expired": 300, "marketing-optout": 240, "vip": 40,
    "test-accounts": 12, "eu-erasure-batch": 20, "eu-erasure-b2": 20, "eu-erasure-b3": 20,
    "eu-erasure-single": 3,
}
TICKETS = [
    ("TKT-1001", "erasure_request", "open"), ("TKT-1002", "erasure_request", "open"),
    ("TKT-1003", "data_request", "open"), ("TKT-1004", "refund_request", "open"),
    ("TKT-1005", "refund_request", "closed"), ("TKT-1006", "erasure_request", "open"),
    ("TKT-1007", "refund_request", "open"), ("TKT-1008", "data_request", "open"),
]
PAYMENTS = [("PAY-1", 20000, 0), ("PAY-2", 3000, 0), ("PAY-3", 400000, 0), ("PAY-4", 9000, 8500)]
SERVICES = [("web", "prod", 10), ("worker", "prod", 6), ("web-staging", "staging", 4), ("batch", "prod", 30)]


def default_policy() -> dict[str, Any]:
    return json.loads(resources.files("countersigned").joinpath("default_policy.json").read_text())


def seed_demo(gate: Gate, *, reset: bool = False) -> None:
    if reset:
        gate.db.reset_all()
    gate.activate_policy(default_policy(), actor="seed")
    for aid, grants in AGENTS.items():
        gate.create_principal(aid, "agent", aid, DEMO_KEYS[aid], grants)
    gate.create_principal("alice", "approver", "Alice (approver)", DEMO_KEYS["alice"])
    gate.create_principal("bob", "approver", "Bob (approver)", DEMO_KEYS["bob"])
    gate.create_principal("admin", "admin", "Admin", DEMO_KEYS["admin"])
    with gate.db.transaction() as conn:
        if conn.execute("SELECT 1 FROM sim_customers LIMIT 1").fetchone():
            return
        for seg, n in SEGMENTS.items():
            conn.execute(
                "INSERT INTO sim_customers (segment, email) SELECT %s, 'user' || g || '@example.test' FROM generate_series(1, %s) g",
                (seg, n),
            )
        for t in TICKETS:
            conn.execute("INSERT INTO sim_tickets (id, kind, status) VALUES (%s, %s, %s)", t)
        for p in PAYMENTS:
            conn.execute("INSERT INTO sim_payments (id, amount_cents, refunded_cents) VALUES (%s, %s, %s)", p)
        for s in SERVICES:
            conn.execute("INSERT INTO sim_services (name, environment, replicas) VALUES (%s, %s, %s)", s)
