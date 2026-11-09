"""The shared fixtures (fixtures/conformance.json) pin the engine and the hashing. The same file is
checked by the TypeScript port in web/test/conformance.test.ts."""

import json
from pathlib import Path

import pytest

from countersigned.audit import chain_hash, verify_rows
from countersigned.canonical import action_hash, canonical_json, sha256_hex
from countersigned.engine import replay
from countersigned.seed import default_policy
from countersigned.service import policy_version_of

FX = json.loads((Path(__file__).parent.parent / "fixtures" / "conformance.json").read_text())
POLICY = default_policy()


def test_policy_version_is_stable():
    assert policy_version_of(POLICY) == FX["policy_version"]


@pytest.mark.parametrize("case", FX["decisions"], ids=lambda c: c["name"])
def test_engine_matches_fixture(case):
    got = replay(case["operation"], case["params"], case["context"], POLICY, FX["policy_version"]).to_dict()
    assert got == case["expected"]


@pytest.mark.parametrize("c", FX["canonical"], ids=lambda c: c["canonical"][:30])
def test_canonical_json_and_sha(c):
    assert canonical_json(c["input"]) == c["canonical"]
    assert sha256_hex(c["canonical"]) == c["sha256"]


@pytest.mark.parametrize("a", FX["action_hashes"], ids=lambda a: a["operation"])
def test_action_hashes(a):
    assert action_hash(a["agent"], a["operation"], a["params"]) == a["hash"]


def test_chain_vector():
    assert verify_rows(FX["chain"]).ok
    for r in FX["chain"]:
        assert chain_hash(r["prev_hash"], r["seq"], r["ts"], r["event_type"], r["action_id"], r["actor"], r["payload"]) == r["hash"]


def test_fixture_covers_every_outcome_and_operation():
    outs = {c["expected"]["outcome"] for c in FX["decisions"]}
    assert outs == {"execute", "ask", "defer", "escalate", "refuse"}
    ops = {c["operation"] for c in FX["decisions"]}
    assert set(POLICY["operations"]) <= ops
