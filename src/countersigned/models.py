"""Wire models and the trust boundary for model output.

A model's output is untrusted text. `parse_proposal` is the only way it becomes a
typed `ActionProposal`: unknown fields are rejected (so `{"approved": true}` is an
error, not a hint), values are restricted to bool / int / short strings, and sizes
are bounded. Identity (who is asking) never comes from the body; it comes from the
authenticated key.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator

Scalar = StrictBool | StrictInt | StrictStr

MAX_RAW_BYTES = 8192
MAX_PARAMS = 16
MAX_STR = 500
JS_SAFE_INT = 2**53 - 1


class ProposalRejected(ValueError):
    """The model output could not be turned into a well-formed proposal."""


class ActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: StrictStr = Field(min_length=3, max_length=80)
    params: dict[StrictStr, Scalar] = Field(default_factory=dict)
    idempotency_key: StrictStr | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("params")
    @classmethod
    def _bounded(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > MAX_PARAMS:
            raise ValueError(f"at most {MAX_PARAMS} params")
        for k, x in v.items():
            if not (1 <= len(k) <= 40) or "\x00" in k:
                raise ValueError(f"bad param name {k!r}")
            if isinstance(x, str) and (len(x) > MAX_STR or "\x00" in x):
                raise ValueError(f"param {k!r} is too long or contains NUL")
            if isinstance(x, int) and not isinstance(x, bool) and abs(x) > JS_SAFE_INT:
                raise ValueError(f"param {k!r} is out of range")
        return v

    @field_validator("operation", "idempotency_key")
    @classmethod
    def _no_nul(cls, v: str | None) -> str | None:
        if v is not None and "\x00" in v:
            raise ValueError("NUL not allowed")
        return v


def parse_proposal(raw: str | bytes | dict[str, Any]) -> ActionProposal:
    """Validate untrusted model output. Raises ProposalRejected, never anything else."""
    try:
        if isinstance(raw, (str, bytes)):
            if len(raw) > MAX_RAW_BYTES:
                raise ProposalRejected("proposal is too large")
            data = json.loads(raw)
        else:
            data = raw
        if not isinstance(data, dict):
            raise ProposalRejected("proposal must be a JSON object")
        return ActionProposal.model_validate(data)
    except ProposalRejected:
        raise
    except (ValidationError, ValueError, TypeError) as e:
        # Keep the message short and free of the offending content.
        errs = e.errors(include_input=False, include_url=False) if isinstance(e, ValidationError) else None
        summary = "; ".join(f"{'.'.join(str(p) for p in x['loc'])}: {x['msg']}" for x in errs[:4]) if errs else str(e)[:200]
        raise ProposalRejected(summary) from None


Outcome = Literal["execute", "ask", "defer", "escalate", "refuse"]
Reversibility = Literal["reversible", "partial", "irreversible"]
Status = Literal[
    "queued", "pending_approval", "approved", "needs_info", "deferred", "refused",
    "executed", "failed", "blocked", "rejected", "expired",
]
