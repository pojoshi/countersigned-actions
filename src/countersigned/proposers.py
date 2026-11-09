"""Where proposals come from.

The gate does not care whether a proposal came from a model, a script or a person:
it only ever sees an `ActionProposal` that survived `parse_proposal`. A real agent
plugs in by implementing `Proposer.propose` (call your model, return its raw JSON
output). The demo and the eval suite use `ScriptedProposer`, so they run the same
way every time and need no API key.
"""

from typing import Any, Protocol

from .models import ActionProposal, ProposalRejected, parse_proposal
from .service import Gate

Raw = str | dict[str, Any]


class Proposer(Protocol):
    name: str

    def propose(self, task: str) -> list[Raw]:
        """Return raw proposals (untrusted model output) for this task."""


class ScriptedProposer:
    def __init__(self, name: str, scripts: dict[str, list[Raw]]) -> None:
        self.name = name
        self.scripts = scripts

    def propose(self, task: str) -> list[Raw]:
        return list(self.scripts.get(task, []))


def submit(gate: Gate, agent_id: str, raw: Raw) -> dict[str, Any]:
    """Run one raw proposal through the trust boundary and then the gate."""
    try:
        proposal: ActionProposal = parse_proposal(raw)
    except ProposalRejected as e:
        gate.record_malformed(agent_id, raw, str(e))
        return {"status": "malformed", "error": str(e)}
    return gate.propose(agent_id, proposal)


def run_task(gate: Gate, agent_id: str, proposer: Proposer, task: str) -> list[dict[str, Any]]:
    return [submit(gate, agent_id, raw) for raw in proposer.propose(task)]
