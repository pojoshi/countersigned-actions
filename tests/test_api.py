import pytest
from fastapi.testclient import TestClient

from countersigned.api import create_app
from countersigned.seed import DEMO_KEYS
from conftest import delete_params


@pytest.fixture
def client(gate):
    return TestClient(create_app(gate))


def h(who):
    return {"X-API-Key": DEMO_KEYS[who]}


def propose(client, who="support-bot", **body):
    body.setdefault("operation", "customer_data.delete_records")
    body.setdefault("params", delete_params())
    return client.post("/v1/actions", json=body, headers=h(who))


def test_keys_are_required_and_roles_are_enforced(client):
    assert client.post("/v1/actions", json={}).status_code == 401
    assert client.post("/v1/actions", json={}, headers={"X-API-Key": "nope"}).status_code == 401
    assert propose(client, who="alice").status_code == 403           # approvers don't propose
    assert client.get("/v1/audit", headers=h("support-bot")).status_code == 403
    assert client.put("/v1/kill-switch", json={"active": True}, headers=h("alice")).status_code == 403


def test_full_flow_over_http(client):
    r = propose(client, params=delete_params(segment="vip", count=40))
    assert r.status_code == 201
    a = r.json()
    assert a["status"] == "pending_approval" and a["decision"]["outcome"] == "escalate"
    d = a["decision"]
    assert {"confidence", "risk_score", "evidence", "missing_information", "reversibility"} <= d.keys()

    assert client.post(f"/v1/actions/{a['id']}/approve", json={"action_hash": "bad"}, headers=h("alice")).status_code == 409
    ok = client.post(f"/v1/actions/{a['id']}/approve", json={"action_hash": a["action_hash"], "note": "ok"}, headers=h("alice"))
    assert ok.json()["status"] == "approved"
    run = client.post("/v1/worker/run", headers=h("admin")).json()
    assert [x["result"] for x in run["results"]] == ["executed"]
    assert client.get(f"/v1/actions/{a['id']}", headers=h("support-bot")).json()["status"] == "executed"
    assert client.get("/v1/audit/verify", headers=h("alice")).json()["ok"] is True
    assert client.get(f"/v1/actions/{a['id']}/replay", headers=h("alice")).json()["matches"] is True


def test_identity_comes_from_the_key_not_the_body(client):
    r = propose(client, agent_id="admin")  # unknown field: rejected, and never used for identity
    assert r.status_code == 422
    r = propose(client)
    assert r.json()["agent_id"] == "support-bot"


def test_malformed_output_is_422_and_audited(client, gate):
    r = client.post("/v1/actions", content=b'{"operation": "payments.refund", "params": {"amount_cents": 2.5}}',
                    headers={**h("finance-bot"), "content-type": "application/json"})
    assert r.status_code == 422
    assert any(e["event_type"] == "malformed_proposal" for e in gate.audit_page())


def test_agents_only_see_their_own_actions(client):
    a = propose(client).json()
    assert client.get(f"/v1/actions/{a['id']}", headers=h("infra-bot")).status_code == 403
    assert client.get(f"/v1/actions/{a['id']}", headers=h("alice")).status_code == 200


def test_kill_switch_over_http(client):
    assert client.put("/v1/kill-switch", json={"active": True, "reason": "drill"}, headers=h("admin")).json()["active"] is True
    assert propose(client).json()["status"] == "refused"
    client.put("/v1/kill-switch", json={"active": False}, headers=h("admin"))
    assert propose(client).json()["status"] == "queued"


def test_idempotent_replays_return_200(client):
    body = {"operation": "customer_data.delete_records", "params": delete_params(), "idempotency_key": "abc"}
    assert propose(client, **body).status_code == 201
    again = propose(client, **body)
    assert again.status_code == 200 and again.json()["duplicate"] is True


def test_inline_worker_executes_in_the_background(gate):
    import time

    with TestClient(create_app(gate, inline_worker=True, poll_seconds=0.05)) as c:
        a = c.post("/v1/actions", json={"operation": "customer_data.delete_records", "params": delete_params()}, headers=h("support-bot")).json()
        for _ in range(60):
            if c.get(f"/v1/actions/{a['id']}", headers=h("support-bot")).json()["status"] == "executed":
                break
            time.sleep(0.05)
        assert c.get(f"/v1/actions/{a['id']}", headers=h("support-bot")).json()["status"] == "executed"
