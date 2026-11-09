"""Two deliberately simple baselines, to show what the gate adds.

B1  allow-list only:   the agent may call the operation or it may not.
B2  guardrail:         B1, plus all required fields present and the quantity *the agent
                       reports* under the per-action limit. This is what many "agent
                       guardrails" amount to in practice.

Neither verifies the agent's claims against data, tracks cumulative use, knows about
reversibility, change windows, ticket validity or a kill switch.
"""

from typing import Any

from countersigned.seed import AGENTS, default_policy

POLICY = default_policy()


def _claimed_quantity(op: str, p: dict[str, Any]) -> int:
    if op in ("customer_data.delete_records", "customer_data.export_records"):
        v = p.get("count")
    elif op in ("payments.refund", "payments.transfer"):
        v = p.get("amount_cents")
    else:
        a, b = p.get("replicas_from"), p.get("replicas_to")
        v = a - b if isinstance(a, int) and isinstance(b, int) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else 10**9


def decide(level: str, agent: str, op: str, p: dict[str, Any]) -> bool:
    """True if the baseline would run the action unattended."""
    grants = [g for g in AGENTS.get(agent, []) if g["operation"] == op]
    if not grants:
        return False
    if level == "B1":
        return True
    spec = POLICY["operations"].get(op)
    if spec is None or any(k not in p for k in spec["required"]):
        return False
    return _claimed_quantity(op, p) <= spec["limits"]["auto"]
