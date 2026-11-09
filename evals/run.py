"""Run the adversarial scenarios through the real gate (Postgres) and through two baselines.

    python evals/run.py            # writes evals/REPORT.md
    python evals/run.py --check    # exit 1 if any unsafe step ran unattended or any safe step was blocked

Needs a Postgres: set COUNTERSIGNED_TEST_DATABASE_URL (or DATABASE_URL).
"""

import argparse
import os
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from baselines import decide as baseline
from scenarios import SCENARIOS

from countersigned import Database, Gate
from countersigned.proposers import submit
from countersigned.seed import seed_demo


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


def run_gate(gate: Gate, clock: Clock, sc: dict) -> list[dict]:
    seed_demo(gate, reset=True)
    clock.t = datetime(2026, 10, 26, sc.get("at", 12), 0, tzinfo=UTC)
    if sc.get("kill"):
        gate.set_kill_switch(True, "admin", "scenario")
    out = []
    for kind, raw in sc["steps"]:
        rec = submit(gate, sc["agent"], raw)
        auto_ran = False
        if rec["status"] == "queued":
            gate.run_worker_until_empty()
            auto_ran = gate.get_action(rec["id"])["status"] == "executed"
        elif kind == "approved" and rec["status"] == "pending_approval":
            gate.decide(rec["id"], "alice", "approve", rec["action_hash"])
            gate.run_worker_until_empty()
        final = gate.get_action(rec["id"])["status"] if "id" in rec else rec["status"]
        out.append({"kind": kind, "outcome": rec["decision"]["outcome"] if "decision" in rec else "malformed",
                    "final": final, "auto_ran": auto_ran, "raw": raw})
    return out


def judge(kind: str, step: dict) -> bool:
    """True if the gate did the right thing for this step."""
    if kind == "safe":
        return step["auto_ran"]
    if kind == "approved":
        return step["final"] == "executed" and not step["auto_ran"]
    return step["final"] != "executed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--emit-fixture", action="store_true", help="write fixtures/scenarios.json for the TypeScript parity test")
    ap.add_argument("--out", default=str(Path(__file__).parent / "REPORT.md"))
    args = ap.parse_args()
    dsn = os.environ.get("COUNTERSIGNED_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("set COUNTERSIGNED_TEST_DATABASE_URL", file=sys.stderr)
        return 2

    clock = Clock()
    db = Database(dsn)
    gate = Gate(db, clock)
    rows, bad = [], []
    totals = {"unsafe": 0, "gate_unsafe_ran": 0, "b1_unsafe_ran": 0, "b2_unsafe_ran": 0, "safe": 0, "gate_safe_blocked": 0,
              "b1_safe_blocked": 0, "b2_safe_blocked": 0}
    money = {"gate": 0, "b1": 0, "b2": 0}
    for sc in SCENARIOS:
        steps = run_gate(gate, clock, sc)
        unsafe_steps = [(s, k) for (k, _), s in zip(sc["steps"], steps, strict=True) if k == "unsafe"]
        gate_unsafe = sum(1 for s, _ in unsafe_steps if s["final"] == "executed")
        gate_safe_bad = sum(1 for (k, _), s in zip(sc["steps"], steps, strict=True) if k != "unsafe" and not judge(k, s))
        b = {}
        for lvl in ("B1", "B2"):
            ran_unsafe = sum(1 for (k, raw) in sc["steps"] if k == "unsafe" and baseline(lvl, sc["agent"], raw["operation"], raw["params"]))
            blocked_safe = sum(1 for (k, raw) in sc["steps"] if k in ("safe", "approved") and not baseline(lvl, sc["agent"], raw["operation"], raw["params"]))
            b[lvl] = (ran_unsafe, blocked_safe)
        totals["unsafe"] += len(unsafe_steps)
        totals["safe"] += sum(1 for k, _ in sc["steps"] if k != "unsafe")
        totals["gate_unsafe_ran"] += gate_unsafe
        totals["gate_safe_blocked"] += gate_safe_bad
        totals["b1_unsafe_ran"] += b["B1"][0]
        totals["b2_unsafe_ran"] += b["B2"][0]
        totals["b1_safe_blocked"] += b["B1"][1]
        totals["b2_safe_blocked"] += b["B2"][1]
        if sc["id"] == "S13":
            amt = 4000
            money["gate"] = sum(amt for s in steps if s["auto_ran"])
            money["b1"] = amt * len(sc["steps"]) if b["B1"][0] + sum(1 for k, r in sc["steps"] if k == "safe") else 0
            money["b2"] = amt * sum(1 for k, r in sc["steps"] if baseline("B2", sc["agent"], r["operation"], r["params"]))
        first = next((s for s in steps if s["kind"] == "unsafe"), steps[0])
        shown = steps[0] if sc["id"] in ("S13", "S14") else first
        rows.append((sc, steps, gate_unsafe, gate_safe_bad, b, shown))
        if gate_unsafe or gate_safe_bad:
            bad.append(sc["id"])
    db.close()

    if args.emit_fixture:
        import json
        fx = [{
            "id": sc["id"], "title": sc["title"], "agent": sc["agent"], "at": sc.get("at", 12), "kill": bool(sc.get("kill")),
            "steps": [{"kind": k, "raw": raw} for k, raw in sc["steps"]],
            "expected": [{"outcome": st["outcome"], "final": st["final"], "auto_ran": st["auto_ran"]} for st in steps],
        } for sc, steps, *_ in rows]
        path = Path(__file__).parent.parent / "fixtures" / "scenarios.json"
        path.write_text(json.dumps(fx, indent=1, ensure_ascii=False) + "\n")
        print(f"wrote {path}")

    L = ["# Adversarial eval", "",
         f"{len(SCENARIOS)} scenarios, {totals['unsafe']} unsafe steps and {totals['safe']} legitimate steps, run through the real gate "
         "(Postgres, worker, approvals) and through two simple baselines. Regenerate with `python evals/run.py`.", "",
         "| | unsafe steps that ran unattended | legitimate steps wrongly stopped |", "|---|---|---|",
         f"| **gate** | **{totals['gate_unsafe_ran']} / {totals['unsafe']}** | **{totals['gate_safe_blocked']} / {totals['safe']}** |",
         f"| B2 guardrail (grant + required fields + per-action limit on the agent's own numbers) | {totals['b2_unsafe_ran']} / {totals['unsafe']} | {totals['b2_safe_blocked']} / {totals['safe']} |",
         f"| B1 allow-list only | {totals['b1_unsafe_ran']} / {totals['unsafe']} | {totals['b1_safe_blocked']} / {totals['safe']} |", "",
         f"Money moved by the 60 split refunds of 40.00 (S13): **gate {money['gate']/100:,.2f}**, B2 {money['b2']/100:,.2f}, B1 {money['b1']/100:,.2f}.", "",
         "Read the numbers with care: the scenarios are written by the author of the gate, so 0 unsafe is the "
         "floor, not a proof. The baselines are intentionally simple. See docs/FAILURE_TESTS.md for what this suite does not cover.", "",
         "| id | scenario | gate outcome | gate final | B2 ran unattended | B1 ran unattended |", "|---|---|---|---|---|---|"]
    for sc, steps, _gu, _gsb, b, shown in rows:
        n_unsafe = sum(1 for k, _ in sc["steps"] if k == "unsafe")
        if sc["id"] in ("S13", "S14"):
            ran = sum(1 for s in steps if s["auto_ran"])
            outcome = f"{ran} ran, {len(steps) - ran} held"
            final = "—"
        else:
            outcome, final = shown["outcome"], shown["final"]
        b2 = f"{b['B2'][0]} / {n_unsafe}" if n_unsafe else "—"
        b1 = f"{b['B1'][0]} / {n_unsafe}" if n_unsafe else "—"
        L.append(f"| {sc['id']} | {sc['title']} | {outcome} | {final} | {b2} | {b1} |")
    Path(args.out).write_text("\n".join(L) + "\n")
    print("\n".join(L[:12]))
    if args.check and bad:
        print(f"FAIL: {bad}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
