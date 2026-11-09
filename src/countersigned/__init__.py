"""countersigned-actions: the model proposes, policy decides, nothing runs without a recorded reason."""

from .audit import VerifyResult, verify_rows
from .db import Database
from .engine import Context, Decision, Grant, AgentInfo, evaluate
from .models import ActionProposal, ProposalRejected, parse_proposal
from .service import Gate

__all__ = [
    "ActionProposal", "AgentInfo", "Context", "Database", "Decision", "Gate", "Grant", "ProposalRejected",
    "VerifyResult", "evaluate", "parse_proposal", "verify_rows",
]
__version__ = "0.1.0"
