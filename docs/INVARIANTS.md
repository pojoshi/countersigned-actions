# Invariants

What the system guarantees, where each guarantee is enforced, and the test that proves it. "Test" names are in `tests/`.

| # | Guarantee | Enforced by | Test |
|---|---|---|---|
| 1 | **No execution without a recorded decision that permits it.** | A `BEFORE INSERT` trigger on `executions` refuses a row unless the action's decision is `execute`, or a matching, unexpired, unconsumed approval exists. Raw SQL can't skip it. | `test_database_refuses_an_execution_nobody_permitted` |
| 2 | **A decision and its audit entry commit together or not at all.** | One transaction: action row, audit entry and outbox row | `test_decision_and_audit_entry_are_one_transaction` |
| 3 | **The audit log can't be edited quietly.** | Triggers refuse `UPDATE`/`DELETE`/`TRUNCATE`; each entry hashes the one before it; `verify` finds the first bad entry | `test_audit_log_cannot_be_edited_with_ordinary_access`, `test_tampering_by_someone_who_bypasses_the_triggers_is_detected`, `test_deleted_or_reordered_entries_are_detected`, `test_any_edit_deletion_or_reorder_is_detected` |
| 4 | **Approvals name the exact action, work once, expire, and never come from the agent.** | `decide()` checks the presented hash, status, approver kind and age; unique index on `approvals(action_id)`; `consumed_at` set in the execution transaction | `test_approval_is_bound_to_the_exact_action`, `test_agents_cannot_approve_and_one_approval_is_one_use`, `test_approval_expires` |
| 5 | **At most one execution per action, even if delivered twice.** | `executions.action_id` primary key; worker ignores actions not in `queued`/`approved` | `test_duplicate_delivery_executes_once`, `test_many_workers_execute_each_action_exactly_once` |
| 6 | **Retrying a proposal doesn't duplicate it.** | `UNIQUE (agent_id, idempotency_key)`; a reused key with different content is a conflict | `test_idempotency_key_returns_the_same_action` |
| 7 | **Every decision can be replayed.** Same stored inputs, same answer. | The `decision` audit entry stores the full context; the policy body is stored by version hash | `test_every_decision_can_be_replayed_from_the_log`, `test_decisions_are_deterministic_and_replayable` |
| 8 | **The policy version is pinned per decision**, and execution re-checks under the current one. | `actions.policy_version`; worker evaluates with the active policy | `test_policy_tightened_after_approval_applies_at_execution` |
| 9 | **Authorization is re-checked at execution time**: kill switch, agent disabled, grants, facts, windows. | `Gate._execute` re-runs `evaluate` in `execute` mode | `test_kill_switch_stops_queued_work`, `test_kill_switch_beats_an_approval`, `test_disabled_agent_and_revoked_grant_block_already_queued_actions`, `test_data_that_changed_after_approval_blocks_the_execution` |
| 10 | **The effect only does what was decided.** | Effects check their own row counts inside a savepoint; a mismatch rolls back and blocks | `test_effect_guard_catches_a_race_the_policy_check_missed` |
| 11 | **Cumulative limits are exact under concurrency**, and later arrivals can't retroactively block earlier actions. | Decision lock; window sums only count earlier actions at execution | `test_concurrent_proposals_cannot_overshoot_a_window`, `test_earlier_actions_are_not_blocked_by_later_arrivals`, `test_splitting_never_gets_more_through_than_the_window_allows` |
| 12 | **Once the kill switch commits, nothing slips past it.** | Workers hold the switch `FOR SHARE`; toggling takes `FOR UPDATE` | `test_kill_switch_races_with_workers_and_nothing_slips_through` |
| 13 | **Model output can't grant itself anything.** | Strict parse; unknown fields rejected; identity from the key | `test_malformed_model_output_never_becomes_an_action`, `test_model_output_cannot_smuggle_approval_or_identity`, `test_parse_proposal_only_ever_raises_proposal_rejected` |
| 14 | **No grant, no execution; kill switch beats everything.** | `evaluate` | `test_nothing_executes_without_a_grant`, `test_kill_switch_overrides_everything_including_approvals` |
| 15 | **The engine never crashes on weird input.** | Resolvers validate types before using values | `test_engine_never_crashes_on_weird_model_output` |

## What these don't cover

- **Throughput.** Invariant 11 costs a global decision lock.
- **External side effects.** Effects here share the database with the log, so 1, 2 and 10 are easy. A real payment API needs its own idempotency keys; the gate can pass the action id along, and that's all.
- **The database owner.** A superuser can disable triggers and rewrite the log. Invariant 3 is "detectable", and only if the head hash is kept elsewhere.
- **A bug in a resolver.** The tests pin behaviour; they can't tell you the resolver models the real system correctly.
