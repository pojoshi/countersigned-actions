"""HTTP API. Identity comes from the X-API-Key header, never from the request body."""

import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .db import Database
from .errors import Forbidden, GateError, Unauthorized
from .models import ProposalRejected, parse_proposal
from .service import Gate


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_hash: str
    note: str | None = None


class KillSwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    reason: str | None = None


def create_app(gate: Gate, *, inline_worker: bool = False, poll_seconds: float = 0.5) -> FastAPI:
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                gate.release_due()
                gate.expire_stale()
                if gate.run_worker_once() is None:
                    time.sleep(poll_seconds)
            except Exception:
                time.sleep(poll_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        t = None
        if inline_worker:
            t = threading.Thread(target=loop, daemon=True, name="countersigned-worker")
            t.start()
        yield
        stop.set()

    app = FastAPI(title="countersigned-actions", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(GateError)
    async def _gate_error(_: Request, exc: GateError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    def principal(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
        if not x_api_key:
            raise Unauthorized("missing X-API-Key")
        return gate.authenticate(x_api_key)

    def require(*kinds: str):
        def dep(p: dict[str, Any] = Depends(principal)) -> dict[str, Any]:
            if p["kind"] not in kinds:
                raise Forbidden(f"{p['kind']} keys cannot do this")
            return p
        return dep

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/policy")
    def policy(_: dict[str, Any] = Depends(principal)) -> dict[str, Any]:
        with gate.db.transaction() as conn:
            body, version = gate.active_policy(conn)
        return {"version": version, "policy": body}

    @app.post("/v1/actions")
    async def propose(request: Request, p: dict[str, Any] = Depends(require("agent"))) -> JSONResponse:
        raw = await request.body()
        try:
            proposal = parse_proposal(raw)
        except ProposalRejected as e:
            await run_in_threadpool(gate.record_malformed, p["id"], raw.decode("utf-8", "replace"), str(e))
            return JSONResponse({"error": f"malformed proposal: {e}"}, status_code=422)
        rec = await run_in_threadpool(gate.propose, p["id"], proposal)
        return JSONResponse(rec, status_code=200 if rec.get("duplicate") else 201)

    @app.get("/v1/actions")
    def list_actions(
        status: str | None = None, limit: int = Query(100, ge=1, le=500),
        _: dict[str, Any] = Depends(require("approver", "admin")),
    ) -> list[dict[str, Any]]:
        return gate.list_actions(status, limit)

    @app.get("/v1/actions/{action_id}")
    def get_action(action_id: str, p: dict[str, Any] = Depends(principal)) -> dict[str, Any]:
        rec = gate.get_action(action_id)
        if p["kind"] == "agent" and rec["agent_id"] != p["id"]:
            raise Forbidden("agents can only read their own actions")
        return rec

    @app.post("/v1/actions/{action_id}/approve")
    def approve(action_id: str, body: Verdict, p: dict[str, Any] = Depends(require("approver", "admin"))) -> dict[str, Any]:
        return gate.decide(action_id, p["id"], "approve", body.action_hash, body.note)

    @app.post("/v1/actions/{action_id}/reject")
    def reject(action_id: str, body: Verdict, p: dict[str, Any] = Depends(require("approver", "admin"))) -> dict[str, Any]:
        return gate.decide(action_id, p["id"], "reject", body.action_hash, body.note)

    @app.get("/v1/actions/{action_id}/replay")
    def replay(action_id: str, _: dict[str, Any] = Depends(require("approver", "admin"))) -> dict[str, Any]:
        return gate.replay_decision(action_id)

    @app.get("/v1/kill-switch")
    def get_kill(_: dict[str, Any] = Depends(principal)) -> dict[str, Any]:
        return gate.kill_switch()

    @app.put("/v1/kill-switch")
    def put_kill(body: KillSwitchBody, p: dict[str, Any] = Depends(require("admin"))) -> dict[str, Any]:
        return gate.set_kill_switch(body.active, p["id"], body.reason)

    @app.post("/v1/agents/{agent_id}/disable")
    def disable(agent_id: str, p: dict[str, Any] = Depends(require("admin"))) -> dict[str, str]:
        gate.set_agent_disabled(agent_id, True, p["id"])
        return {"agent": agent_id, "disabled": "true"}

    @app.post("/v1/agents/{agent_id}/enable")
    def enable(agent_id: str, p: dict[str, Any] = Depends(require("admin"))) -> dict[str, str]:
        gate.set_agent_disabled(agent_id, False, p["id"])
        return {"agent": agent_id, "disabled": "false"}

    @app.get("/v1/audit")
    def audit_page(
        after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000),
        _: dict[str, Any] = Depends(require("approver", "admin")),
    ) -> list[dict[str, Any]]:
        return gate.audit_page(after, limit)

    @app.get("/v1/audit/verify")
    def audit_verify(_: dict[str, Any] = Depends(require("approver", "admin"))) -> dict[str, Any]:
        return gate.verify_audit()

    @app.post("/v1/worker/run")
    def run_worker(_: dict[str, Any] = Depends(require("admin"))) -> dict[str, Any]:
        released = gate.release_due()
        expired = gate.expire_stale()
        results = gate.run_worker_until_empty()
        return {"released": released, "expired": expired, "results": results}

    return app


def create_app_from_env() -> FastAPI:
    from .seed import default_policy, seed_demo

    gate = Gate(Database(os.environ["DATABASE_URL"]))
    if os.environ.get("SEED_DEMO", "1") == "1":
        seed_demo(gate)
    else:
        gate.activate_policy(default_policy())
    return create_app(gate, inline_worker=os.environ.get("WORKER", "inline") == "inline")
