# countersigned-actions

**A policy gate and audit trail for AI agents. The model proposes, policy decides, and nothing runs without a recorded reason.**

**[Live demo](https://pojoshi.github.io/countersigned-actions/)**: try to get an agent's action past the gate. No install, no sign-up.

An agent shouldn't get to delete 500 customer records because a model asked nicely. This sits between the proposal and the action: checks who's asking, what they actually want to touch, whether it can be undone, how much that agent has already done today. Then it runs the thing, sends it back, holds it for a human, or refuses. Either way it writes down why.

```
Delete 500 customer records?      → Needs a human   risk 70, 500 affected, reversible, ticket verified
Delete "10" (the segment has 600) → Refused         believes the data, not the agent
Cite ticket TKT-9999              → Sent back       that ticket doesn't exist
60 refunds of 40.00, one by one   → 5 run, 55 held   each is under the limit; the day's total isn't
```

## Why

I spend my days building AI pipelines: classification, detection, cost control. What worries me isn't the model, it's the moment its output quietly becomes an action nobody can inspect later. This is the gate I'd want standing in front of it. Deterministic, replayable, and upfront about where it still falls short.

## What a decision contains

Five outcomes: **execute · ask · defer · escalate · refuse**.

| | |
|---|---|
| confidence | 0–100, not a probability. Drops when the agent's claim disagreed with reality, or the action sits right under a limit |
| risk score | 0–100, a sum you can read back: reversibility, size, PII, environment, cumulative use, claim mismatch |
| evidence | each line tagged by source: policy, verified fact, agent claim, system state |
| missing information | exactly what to fix before trying again |
| reversibility | reversible, partial, or irreversible |

Five operations across three domains (customer data, payments, infrastructure), against a small simulated business so there's something real to protect. Every decision stores everything it depended on, so it can be **replayed**: same inputs, same answer.

## Run it

**Docker** (API on :8000, Postgres, demo agents):

```bash
git clone https://github.com/pojoshi/countersigned-actions && cd countersigned-actions
docker compose up --build

# the support agent proposes erasing 40 VIP records (too many to run on its own)
curl -s localhost:8000/v1/actions -H 'X-API-Key: demo-key-support-bot' -H 'content-type: application/json' \
  -d '{"operation":"customer_data.delete_records","params":{"segment":"vip","count":40,"ticket_id":"TKT-1006","reason":"erasure request"}}'
# approve exactly that action (use its action_hash), as a human
curl -s -X POST localhost:8000/v1/actions/<id>/approve -H 'X-API-Key: demo-key-alice' -H 'content-type: application/json' \
  -d '{"action_hash":"<action_hash>"}'
curl -s localhost:8000/v1/audit/verify -H 'X-API-Key: demo-key-alice'
```

**Tests and evals** (needs its own Postgres, since the tests wipe whatever tables they use):

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
docker run -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=countersigned_test -p 5432:5432 postgres:16
export COUNTERSIGNED_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/countersigned_test
pytest                       # 148 tests: unit, property-based, concurrency, tamper, API, known gaps
python evals/run.py          # adversarial suite, writes evals/REPORT.md
cd web && pnpm install --frozen-lockfile && pnpm run check   # TypeScript port + the demo page
```

`.env.example` has the API container's settings. Demo keys are public on purpose, and only exist when `SEED_DEMO=1`.

The Docker stack above is a real run, not a dry read: build, propose, escalate, approve, execute, verify. The CI workflow is the same — `act -j python` and `act -j playground` pass against the committed workflow file. pages.yml's install-test-build steps pass too; `configure-pages` and the deploy job call the real GitHub API to check Pages is enabled, so those only run for real once this repo exists there.

## How it works

![architecture](docs/architecture.svg)

- **Model output is data.** `parse_proposal` takes a strict shape and rejects everything else, including `{"approved": true}`. Identity comes from the API key, never the body.
- **The decision is a pure function.** `evaluate(operation, params, context)` has no I/O, no clock, no model. The service gathers the facts (ticket, segment size, payment, service) and the agent's recent usage; the engine just decides. The agent's count is a claim, the blast radius is computed from the data.
- **Decision and audit commit together.** One transaction: the action, a hash-chained audit entry, an outbox row.
- **Humans sign the exact action.** An approval names the action's hash, works once, expires, and can't come from the agent that proposed it.
- **Everything gets re-checked at the last second.** The worker re-evaluates under the current policy with the kill switch locked, then the effect verifies the rows it's touching still match what was approved.

More: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Every guarantee, with the test that proves it: [docs/INVARIANTS.md](docs/INVARIANTS.md).

## Does it work?

25 adversarial scenarios (72 unsafe steps, 14 legitimate ones) run through the real service, against two deliberately simple baselines:

| | unsafe steps that ran unattended | legitimate steps wrongly stopped |
|---|---|---|
| **this gate** | **0 / 72** | **0 / 14** |
| guardrail: grant + per-action limit on the agent's own numbers | 68 / 72 | 1 / 14 |
| allow-list only | 70 / 72 | 0 / 14 |

I wrote both the scenarios and the gate, so 0 is a floor on what I thought to try, not a proof. Full table: [evals/REPORT.md](evals/REPORT.md). I also broke the engine five specific ways on purpose: dropped the window check, ignored grants, ignored the kill switch, trusted the agent's count, let approval lift a refusal. The suite failed every time.

## Where it breaks

Full writeup: [docs/FAILURE_TESTS.md](docs/FAILURE_TESTS.md). Short version:

- **Caught**: splitting one big job into small ones. 60 refunds of 40.00 each clear the per-action limit; the 24-hour total doesn't.
- **Not caught**: the allowed amount every day for ten days straight, two agents each staying under their own limit, one real ticket covering unrelated deletions. These are tests too, so the gaps are executable, not just a disclaimer.

Also worth knowing:
- The simulated business stands in for a real one. Plugging in a real system means writing its resolvers and effects.
- Effects share a database with the log, so they're atomic with it for free. A real external side effect needs its own idempotency key.
- One global lock keeps the window arithmetic exact. That caps throughput; shard it by domain if that ever matters.
- A crashing effect could stall the queue head. No dead-letter handling yet.
- No LLM proposer ships with this. Scripted proposers keep the demo reproducible and key-free; `Proposer` is the seam for plugging a model in.
- The browser demo runs a TypeScript port, not the Postgres service. The same 57 decision cases and 25 scenarios pass on both, but the port reads `3.0` as `3`.
- The audit log is tamper-evident, not immutable. Full database access lets someone rewrite the chain, unless the latest hash is also kept somewhere they can't reach.

## Layout

```
src/countersigned/   engine.py (pure decision) · service.py (transactions, worker) · audit.py · api.py · schema.sql · default_policy.json (the policy)
tests/               unit · property-based · concurrency · tamper · API · conformance · known gaps
evals/               adversarial scenarios, baselines, generated report
fixtures/            conformance cases shared with the TypeScript port
web/                 TypeScript port of the engine + in-memory gate, and the demo page
docs/                architecture, invariants, failure tests, two-year thesis
```

## Background

The decision-outcome shape started from a hackathon brief on agent decision layers: execute / ask / defer / escalate / refuse, with confidence, risk, evidence, and reversibility. Everything past that starting shape is original to this build.

MIT licensed. See [LICENSE](LICENSE).
