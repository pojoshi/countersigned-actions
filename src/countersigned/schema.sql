-- countersigned-actions schema. Idempotent: safe to run on every start.

CREATE TABLE IF NOT EXISTS policies (
    version    text PRIMARY KEY,                 -- "p-" + first 12 hex of sha256(canonical body)
    body       jsonb NOT NULL,
    active     boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS policies_one_active ON policies ((true)) WHERE active;

CREATE TABLE IF NOT EXISTS principals (
    id       text PRIMARY KEY,
    kind     text NOT NULL CHECK (kind IN ('agent', 'approver', 'admin')),
    name     text NOT NULL,
    key_hash text NOT NULL UNIQUE,               -- sha256 of the API key; the key itself is never stored
    grants   jsonb NOT NULL DEFAULT '[]',        -- agents only: [{operation, environments, max_quantity}]
    disabled boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS kill_switch (
    id     int PRIMARY KEY CHECK (id = 1),
    active boolean NOT NULL,
    reason text,
    set_by text,
    set_at timestamptz
);
INSERT INTO kill_switch (id, active) VALUES (1, false) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS actions (
    seq             bigserial UNIQUE,            -- arrival order; window checks only count earlier actions
    id              text PRIMARY KEY,
    agent_id        text NOT NULL REFERENCES principals (id),
    operation       text NOT NULL,
    params          jsonb NOT NULL,
    action_hash     text NOT NULL,               -- sha256 over (agent, operation, params): what approvals bind to
    idempotency_key text,
    status          text NOT NULL CHECK (status IN (
        'queued', 'pending_approval', 'approved', 'needs_info', 'deferred', 'refused',
        'executed', 'failed', 'blocked', 'rejected', 'expired')),
    decision        jsonb NOT NULL,
    policy_version  text NOT NULL REFERENCES policies (version),
    quantity        bigint NOT NULL DEFAULT 0,   -- effective quantity, counted toward windows
    not_before      timestamptz,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    UNIQUE (agent_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS actions_window_idx ON actions (operation, created_at);
CREATE INDEX IF NOT EXISTS actions_status_idx ON actions (status);

CREATE TABLE IF NOT EXISTS approvals (
    id          text PRIMARY KEY,
    action_id   text NOT NULL REFERENCES actions (id),
    action_hash text NOT NULL,
    approver_id text NOT NULL REFERENCES principals (id),
    verdict     text NOT NULL CHECK (verdict IN ('approve', 'reject')),
    note        text,
    created_at  timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL,
    consumed_at timestamptz
);
-- One approval per action, and it can only ever be used once (see executions).
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_per_action ON approvals (action_id);

CREATE TABLE IF NOT EXISTS executions (
    action_id   text PRIMARY KEY REFERENCES actions (id),   -- at most one execution per action
    approval_id text REFERENCES approvals (id),
    started_at  timestamptz NOT NULL,
    result      jsonb NOT NULL
);

-- Invariant 1, enforced by the database and not only by application code: an
-- execution row cannot exist unless a recorded decision or a matching, unexpired,
-- unconsumed approval permits it.
CREATE OR REPLACE FUNCTION enforce_execution_permitted() RETURNS trigger AS $$
DECLARE a actions%ROWTYPE;
BEGIN
    SELECT * INTO a FROM actions WHERE id = NEW.action_id;
    IF a.id IS NULL THEN
        RAISE EXCEPTION 'execution of unknown action %', NEW.action_id USING ERRCODE = '23514';
    END IF;
    IF NEW.approval_id IS NULL THEN
        IF a.decision ->> 'outcome' = 'execute' THEN RETURN NEW; END IF;
    ELSIF EXISTS (
        SELECT 1 FROM approvals p
         WHERE p.id = NEW.approval_id AND p.action_id = NEW.action_id AND p.verdict = 'approve'
           AND p.action_hash = a.action_hash AND p.consumed_at IS NULL AND p.expires_at > NEW.started_at
    ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'execution of % has no recorded decision permitting it', NEW.action_id USING ERRCODE = '23514';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS executions_permitted ON executions;
CREATE TRIGGER executions_permitted BEFORE INSERT ON executions
    FOR EACH ROW EXECUTE FUNCTION enforce_execution_permitted();

CREATE TABLE IF NOT EXISTS outbox (
    id           bigserial PRIMARY KEY,
    action_id    text NOT NULL REFERENCES actions (id),
    topic        text NOT NULL,
    created_at   timestamptz NOT NULL,
    processed_at timestamptz,
    attempts     int NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (id) WHERE processed_at IS NULL;

-- Hash-chained audit log. Tamper-evident, not immutable: triggers refuse
-- UPDATE/DELETE/TRUNCATE, and every row commits to the one before it, so an edit by
-- someone who bypasses the triggers (a superuser) is detected by `verify`.
CREATE TABLE IF NOT EXISTS audit_log (
    seq        bigint PRIMARY KEY,
    ts         text NOT NULL,
    event_type text NOT NULL,
    action_id  text,
    actor      text NOT NULL,
    payload    jsonb NOT NULL,
    prev_hash  text NOT NULL,
    hash       text NOT NULL UNIQUE
);
CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = '42501';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_no_update ON audit_log;
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
DROP TRIGGER IF EXISTS audit_no_truncate ON audit_log;
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();

-- A small simulated business, so there is something real to protect.
CREATE TABLE IF NOT EXISTS sim_customers (
    id         bigserial PRIMARY KEY,
    segment    text NOT NULL,
    email      text NOT NULL,
    deleted_at timestamptz
);
CREATE INDEX IF NOT EXISTS sim_customers_segment ON sim_customers (segment) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS sim_tickets (
    id     text PRIMARY KEY,
    kind   text NOT NULL,
    status text NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_payments (
    id             text PRIMARY KEY,
    amount_cents   bigint NOT NULL,
    refunded_cents bigint NOT NULL DEFAULT 0 CHECK (refunded_cents <= amount_cents)
);
CREATE TABLE IF NOT EXISTS sim_services (
    name        text PRIMARY KEY,
    environment text NOT NULL,
    replicas    int NOT NULL CHECK (replicas >= 0)
);
CREATE TABLE IF NOT EXISTS sim_exports (
    id          bigserial PRIMARY KEY,
    action_id   text NOT NULL,
    destination text NOT NULL,
    row_count   bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_transfers (
    id           bigserial PRIMARY KEY,
    action_id    text NOT NULL,
    to_account   text NOT NULL,
    amount_cents bigint NOT NULL
);
