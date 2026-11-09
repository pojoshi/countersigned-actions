// An in-memory gate that mirrors src/countersigned/service.py closely enough that the same
// scenarios give the same decisions and final statuses (test/scenarios.test.ts checks this
// against what the Python service did on Postgres). It has no database, so it cannot show
// real concurrency; the Python service and its tests cover that.
import worldJson from '../../fixtures/world.json';
import { actionHash, canonicalJson, chainHash, GENESIS, verifyRows, type AuditRow, type Json, type VerifyResult } from './canonical';
import {
  evaluate, type AgentInfo, type Context, type Decision, type Facts, type Params, type Policy, type StoredContext,
} from './engine';
import { DEFAULT_POLICY, DEFAULT_POLICY_VERSION } from './policy';

export class GateError extends Error {
  constructor(public kind: 'not_found' | 'conflict' | 'forbidden' | 'expired', message: string) { super(message); }
}
export class ProposalRejected extends Error {}

export interface Proposal { operation: string; params: Params; idempotency_key?: string }

const MAX_RAW = 8192;
const SAFE_INT = 2 ** 53 - 1;

/** The trust boundary for model output. Mirrors models.parse_proposal. */
export function parseProposal(raw: unknown): Proposal {
  let data: unknown = raw;
  if (typeof raw === 'string') {
    if (raw.length > MAX_RAW) throw new ProposalRejected('proposal is too large');
    try { data = JSON.parse(raw); } catch { throw new ProposalRejected('not valid JSON'); }
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) throw new ProposalRejected('proposal must be a JSON object');
  const o = data as Record<string, unknown>;
  const errs: string[] = [];
  for (const k of Object.keys(o)) if (!['operation', 'params', 'idempotency_key'].includes(k)) errs.push(`${k}: Extra inputs are not permitted`);
  const op = o.operation;
  if (typeof op !== 'string' || op.length < 3 || op.length > 80 || op.includes('\0')) errs.push('operation: must be a string of 3 to 80 characters');
  const params: Params = {};
  const p = o.params ?? {};
  if (typeof p !== 'object' || p === null || Array.isArray(p)) errs.push('params: must be an object');
  else {
    const keys = Object.keys(p);
    if (keys.length > 16) errs.push('params: at most 16 params');
    for (const k of keys) {
      const v = (p as Record<string, unknown>)[k];
      if (k.length < 1 || k.length > 40 || k.includes('\0')) errs.push(`params: bad param name ${JSON.stringify(k)}`);
      else if (typeof v === 'boolean') params[k] = v;
      else if (typeof v === 'number') {
        if (!Number.isInteger(v)) errs.push(`params.${k}: Input should be a valid integer`);
        else if (Math.abs(v) > SAFE_INT) errs.push(`params.${k}: out of range`);
        else params[k] = v;
      } else if (typeof v === 'string') {
        if (v.length > 500 || v.includes('\0')) errs.push(`params.${k}: too long or contains NUL`);
        else params[k] = v;
      } else errs.push(`params.${k}: must be a boolean, integer or string`);
    }
  }
  const idem = o.idempotency_key;
  if (idem !== undefined && idem !== null && (typeof idem !== 'string' || idem.length < 1 || idem.length > 120 || idem.includes('\0'))) {
    errs.push('idempotency_key: must be a string of 1 to 120 characters');
  }
  if (errs.length) throw new ProposalRejected(errs.slice(0, 4).join('; '));
  const out: Proposal = { operation: op as string, params };
  if (typeof idem === 'string') out.idempotency_key = idem;
  return out;
}

export type Status =
  | 'queued' | 'pending_approval' | 'approved' | 'needs_info' | 'deferred' | 'refused'
  | 'executed' | 'failed' | 'blocked' | 'rejected' | 'expired';

const STATUS_FOR: Record<string, Status> = {
  execute: 'queued', escalate: 'pending_approval', ask: 'needs_info', defer: 'deferred', refuse: 'refused',
};
const COUNTED: Status[] = ['queued', 'pending_approval', 'approved', 'executed'];

export interface Action {
  seq: number;
  id: string;
  agent_id: string;
  operation: string;
  params: Params;
  action_hash: string;
  idempotency_key?: string;
  status: Status;
  decision: Decision;
  policy_version: string;
  quantity: number;
  not_before: string | null;
  created_at: string;
  updated_at: string;
  duplicate?: boolean;
}

interface Approval {
  id: string; action_id: string; action_hash: string; approver_id: string; verdict: 'approve' | 'reject';
  note: string | null; created_at: string; expires_at: string; consumed_at: string | null;
}

interface World {
  customers: Record<string, { live: number; total: number }>;
  tickets: Record<string, { kind: string; status: string }>;
  payments: Record<string, { amount_cents: number; refunded_cents: number }>;
  services: Record<string, { environment: string; replicas: number }>;
  exports: { action_id: string; destination: string; rows: number }[];
  transfers: { action_id: string; to_account: string; amount_cents: number }[];
}

export type ExecResult =
  | { action_id: string; result: 'executed'; effect: Record<string, Json> }
  | { action_id: string; result: 'blocked'; why: string }
  | { action_id: string; result: 'ignored' };

class EffectBlocked extends Error {}

export function newWorld(): World {
  const w: World = { customers: {}, tickets: {}, payments: {}, services: {}, exports: [], transfers: [] };
  for (const [seg, n] of Object.entries(worldJson.segments as Record<string, number>)) w.customers[seg] = { live: n, total: n };
  for (const t of worldJson.tickets) w.tickets[t.id] = { kind: t.kind, status: t.status };
  for (const p of worldJson.payments) w.payments[p.id] = { amount_cents: p.amount_cents, refunded_cents: p.refunded_cents };
  for (const s of worldJson.services) w.services[s.name] = { environment: s.environment, replicas: s.replicas };
  return w;
}

export const AGENT_GRANTS = worldJson.agents as unknown as Record<string, AgentInfo['grants']>;
export const APPROVERS = ['alice', 'bob'];

const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

export class Sim {
  clock: Date;
  policy: Policy = DEFAULT_POLICY;
  policyVersion = DEFAULT_POLICY_VERSION;
  world: World = newWorld();
  agents: Record<string, AgentInfo> = {};
  actions: Action[] = [];
  approvals: Approval[] = [];
  outbox: { action_id: string; done: boolean }[] = [];
  audit: AuditRow[] = [];
  kill: { active: boolean; reason: string | null } = { active: false, reason: null };
  private n = 0;

  constructor(start: Date = new Date(Date.UTC(2026, 9, 26, 12, 0, 0))) {
    this.clock = start;
    for (const [id, grants] of Object.entries(AGENT_GRANTS)) this.agents[id] = { id, disabled: false, grants: clone(grants) };
    this.log('policy_activated', 'seed', { version: this.policyVersion, previous: null });
  }

  // ------------------------------------------------------------------ time
  now(): Date { return this.clock; }
  setTime(d: Date): void { this.clock = d; }
  advance(ms: number): void { this.clock = new Date(this.clock.getTime() + ms); }
  private id(prefix: string): string { return `${prefix}_${String(++this.n).padStart(6, '0')}`; }

  // ----------------------------------------------------------------- audit
  private log(eventType: string, actor: string, payload: Json, actionId: string | null = null): void {
    const last = this.audit[this.audit.length - 1];
    const seq = last ? last.seq + 1 : 1;
    const prev = last ? last.hash : GENESIS;
    const ts = this.clock.toISOString();
    const hash = chainHash(prev, seq, ts, eventType, actionId, actor, payload);
    this.audit.push({ seq, ts, event_type: eventType, action_id: actionId, actor, payload, prev_hash: prev, hash });
  }
  verify(): VerifyResult { return verifyRows(this.audit); }
  head(): string { return this.audit[this.audit.length - 1]?.hash ?? GENESIS; }

  /** Edit an entry the way someone with direct database access could. For the demo. */
  tamper(seq: number): void {
    const row = this.audit.find((r) => r.seq === seq);
    if (!row) return;
    const p = clone(row.payload) as { [k: string]: Json };
    if (p && typeof p === 'object' && !Array.isArray(p)) {
      const d = p.decision as { [k: string]: Json } | undefined;
      if (d && typeof d === 'object') d.risk_score = 1; else p.tampered = true;
    }
    row.payload = p;
  }

  // ---------------------------------------------------------------- facts
  private gatherFacts(operation: string, p: Params): Facts {
    const f: Facts = {};
    const tid = p.ticket_id;
    if (typeof tid === 'string' && this.world.tickets[tid]) f.tickets = { [tid]: { ...this.world.tickets[tid]! } };
    const seg = p.segment;
    if (operation === 'customer_data.delete_records' && typeof seg === 'string') {
      const c = this.world.customers[seg];
      if (c && c.total > 0) f.segment_sizes = { [seg]: c.live };
    }
    const pid = p.payment_id;
    if (operation === 'payments.refund' && typeof pid === 'string' && this.world.payments[pid]) f.payments = { [pid]: { ...this.world.payments[pid]! } };
    const svc = p.service;
    if (operation === 'infrastructure.scale_down' && typeof svc === 'string' && this.world.services[svc]) f.services = { [svc]: this.world.services[svc]!.replicas };
    return f;
  }

  private usage(agentId: string, operation: string, hours: number, beforeSeq?: number, excludeId?: string): [number, number] {
    const since = this.clock.getTime() - hours * 3600000;
    let a = 0, g = 0;
    for (const x of this.actions) {
      if (x.operation !== operation || !COUNTED.includes(x.status)) continue;
      if (new Date(x.created_at).getTime() <= since) continue;
      if (beforeSeq !== undefined && x.seq >= beforeSeq) continue;
      if (excludeId !== undefined && x.id === excludeId) continue;
      g += x.quantity;
      if (x.agent_id === agentId) a += x.quantity;
    }
    return [a, g];
  }

  private context(agent: AgentInfo, operation: string, p: Params, mode: string, humanApproved = false, beforeSeq?: number, excludeId?: string): Context {
    const hours = this.policy.operations[operation]?.window?.hours ?? 24;
    const [ua, ug] = this.usage(agent.id, operation, hours, beforeSeq, excludeId);
    return {
      now: this.clock.toISOString(), agent: clone(agent), facts: this.gatherFacts(operation, p), usage_agent: ua, usage_global: ug,
      kill_switch: this.kill.active, kill_reason: this.kill.reason, mode, human_approved: humanApproved,
      policy: this.policy, policy_version: this.policyVersion,
    };
  }

  private storedInputs(c: Context): StoredContext {
    const { policy: _p, policy_version: _v, ...rest } = c;
    return rest;
  }

  // -------------------------------------------------------------- propose
  /** Submit raw (untrusted) model output. Returns the action, or {malformed} if it never became one. */
  submit(agentId: string, raw: unknown): Action | { malformed: string } {
    let proposal: Proposal;
    try { proposal = parseProposal(raw); } catch (e) {
      const msg = (e as Error).message.slice(0, 300);
      this.log('malformed_proposal', agentId, { raw_sha256: 'withheld', error: msg });
      return { malformed: msg };
    }
    return this.propose(agentId, proposal);
  }

  propose(agentId: string, proposal: Proposal): Action {
    const now = this.clock.toISOString();
    const hash = actionHash(agentId, proposal.operation, proposal.params as Record<string, Json>);
    if (proposal.idempotency_key) {
      const dup = this.actions.find((a) => a.agent_id === agentId && a.idempotency_key === proposal.idempotency_key);
      if (dup) {
        if (dup.action_hash !== hash) throw new GateError('conflict', 'idempotency key was already used for a different action');
        return { ...dup, duplicate: true };
      }
    }
    const agent = this.agents[agentId];
    if (!agent) throw new GateError('not_found', `no agent ${agentId}`);
    const ctx = this.context(agent, proposal.operation, proposal.params, 'propose');
    const dec = evaluate(proposal.operation, proposal.params, ctx);
    const status = STATUS_FOR[dec.outcome]!;
    const a: Action = {
      seq: this.actions.length + 1, id: this.id('act'), agent_id: agentId, operation: proposal.operation, params: proposal.params,
      action_hash: hash, status, decision: dec, policy_version: this.policyVersion, quantity: dec.effective_quantity ?? 0,
      not_before: dec.retry_after, created_at: now, updated_at: now,
    };
    if (proposal.idempotency_key) a.idempotency_key = proposal.idempotency_key;
    this.actions.push(a);
    this.log('decision', agentId, {
      action_id: a.id, operation: a.operation, params: a.params, action_hash: hash, status,
      decision: dec as unknown as Json, inputs: this.storedInputs(ctx) as unknown as Json,
    }, a.id);
    if (status === 'queued') this.outbox.push({ action_id: a.id, done: false });
    return a;
  }

  get(id: string): Action {
    const a = this.actions.find((x) => x.id === id);
    if (!a) throw new GateError('not_found', `no action ${id}`);
    return a;
  }

  // ------------------------------------------------------------ approvals
  decide(actionId: string, approverId: string, verdict: 'approve' | 'reject', presentedHash: string, note: string | null = null): Action {
    const a = this.get(actionId);
    if (!APPROVERS.includes(approverId)) throw new GateError('forbidden', `${approverId} cannot approve actions`);
    if (a.agent_id === approverId) throw new GateError('forbidden', 'an agent cannot approve its own action');
    if (a.status !== 'pending_approval') throw new GateError('conflict', `action is ${a.status}, not pending_approval`);
    if (presentedHash !== a.action_hash) throw new GateError('conflict', 'the hash presented does not match this action: approve exactly what was proposed');
    const ttl = this.policy.approval_ttl_minutes * 60000;
    const now = this.clock.getTime();
    if (now > new Date(a.created_at).getTime() + ttl) {
      a.status = 'expired'; a.updated_at = this.clock.toISOString();
      this.log('approval_expired', 'system', { action_id: a.id }, a.id);
      throw new GateError('expired', 'the approval window has passed; the action is expired and must be re-proposed');
    }
    this.approvals.push({
      id: this.id('apr'), action_id: a.id, action_hash: a.action_hash, approver_id: approverId, verdict, note,
      created_at: this.clock.toISOString(), expires_at: new Date(now + ttl).toISOString(), consumed_at: null,
    });
    a.status = verdict === 'approve' ? 'approved' : 'rejected';
    a.updated_at = this.clock.toISOString();
    this.log(`action_${a.status}`, approverId, { action_id: a.id, action_hash: a.action_hash, note }, a.id);
    if (a.status === 'approved') this.outbox.push({ action_id: a.id, done: false });
    return a;
  }

  expireStale(): number {
    const cutoff = this.clock.getTime() - this.policy.approval_ttl_minutes * 60000;
    let n = 0;
    for (const a of this.actions) {
      if (a.status === 'pending_approval' && new Date(a.created_at).getTime() < cutoff) {
        a.status = 'expired'; a.updated_at = this.clock.toISOString();
        this.log('approval_expired', 'system', { action_id: a.id }, a.id);
        n++;
      }
    }
    return n;
  }

  // ----------------------------------------------------------- kill switch
  setKill(active: boolean, actor: string, reason: string | null = null): void {
    this.kill = { active, reason: active ? reason : null };
    this.log(active ? 'kill_switch_on' : 'kill_switch_off', actor, { reason });
  }

  setAgentDisabled(id: string, disabled: boolean, actor: string): void {
    const a = this.agents[id];
    if (!a) throw new GateError('not_found', `no agent ${id}`);
    a.disabled = disabled;
    this.log(disabled ? 'agent_disabled' : 'agent_enabled', actor, { agent: id });
  }

  // -------------------------------------------------------------- deferral
  releaseDue(): number {
    let n = 0;
    for (const a of this.actions) {
      if (a.status !== 'deferred' || !a.not_before || new Date(a.not_before).getTime() > this.clock.getTime()) continue;
      const ctx = this.context(this.agents[a.agent_id]!, a.operation, a.params, 'propose', false, undefined, a.id);
      const dec = evaluate(a.operation, a.params, ctx);
      a.status = STATUS_FOR[dec.outcome]!; a.decision = dec; a.policy_version = this.policyVersion;
      a.quantity = dec.effective_quantity ?? 0; a.not_before = dec.retry_after; a.updated_at = this.clock.toISOString();
      this.log('redecision', 'system', {
        action_id: a.id, operation: a.operation, params: a.params, action_hash: a.action_hash, status: a.status,
        decision: dec as unknown as Json, inputs: this.storedInputs(ctx) as unknown as Json,
      }, a.id);
      if (a.status === 'queued') this.outbox.push({ action_id: a.id, done: false });
      n++;
    }
    return n;
  }

  // ---------------------------------------------------------------- worker
  runWorker(): ExecResult[] {
    const out: ExecResult[] = [];
    for (let i = 0; i < this.outbox.length; i++) {
      const row = this.outbox[i]!;
      if (row.done) continue;
      out.push(this.execute(row.action_id));
      row.done = true;
    }
    return out;
  }

  private block(a: Action, checked: Json, ctx: Context, why: string): ExecResult {
    a.status = 'blocked'; a.updated_at = this.clock.toISOString();
    this.log('execution_blocked', 'worker', { action_id: a.id, why, recheck: checked, inputs: this.storedInputs(ctx) as unknown as Json }, a.id);
    return { action_id: a.id, result: 'blocked', why };
  }

  private execute(actionId: string): ExecResult {
    const a = this.get(actionId);
    if (a.status !== 'queued' && a.status !== 'approved') {
      this.log('duplicate_delivery_ignored', 'worker', { action_id: actionId, status: a.status }, actionId);
      return { action_id: actionId, result: 'ignored' };
    }
    const now = this.clock.getTime();
    const agent = this.agents[a.agent_id]!;
    let approval: Approval | undefined;
    let humanOk = false;
    if (a.status === 'approved') {
      approval = this.approvals.find((x) => x.action_id === a.id && x.verdict === 'approve');
      humanOk = !!approval && approval.action_hash === a.action_hash && approval.consumed_at === null && new Date(approval.expires_at).getTime() > now;
    }
    const ctx = this.context(agent, a.operation, a.params, 'execute', humanOk, a.seq, a.id);
    const dec = evaluate(a.operation, a.params, ctx);
    const checked = { outcome: dec.outcome, risk_score: dec.risk_score, policy_version: this.policyVersion, reasons: dec.reasons } as unknown as Json;
    if (a.status === 'approved' && !humanOk) return this.block(a, checked, ctx, 'approval is missing, altered, used or expired');
    if (dec.outcome !== 'execute') return this.block(a, checked, ctx, dec.reasons.join('; '));

    let effect: Record<string, Json>;
    try { effect = this.applyEffect(a); } catch (e) {
      if (e instanceof EffectBlocked) return this.block(a, checked, ctx, e.message);
      throw e;
    }
    if (approval) approval.consumed_at = this.clock.toISOString();
    a.status = 'executed'; a.updated_at = this.clock.toISOString();
    this.log('executed', 'worker', {
      action_id: a.id, result: effect, recheck: checked, approval_id: approval?.id ?? null, inputs: this.storedInputs(ctx) as unknown as Json,
    }, a.id);
    return { action_id: a.id, result: 'executed', effect };
  }

  /** Checks first, then changes: nothing is modified unless every check passes (the Postgres version rolls back instead). */
  private applyEffect(a: Action): Record<string, Json> {
    const p = a.params;
    const w = this.world;
    switch (a.operation) {
      case 'customer_data.delete_records': {
        const seg = p.segment as string;
        const c = w.customers[seg]!;
        if (c.live !== p.count) throw new EffectBlocked(`segment '${seg}' changed: ${c.live} rows matched, ${p.count} were approved`);
        const hard = p.hard === true;
        if (hard) c.total -= c.live;
        const deleted = c.live;
        c.live = 0;
        return { deleted, mode: hard ? 'hard' : 'soft', segment: seg };
      }
      case 'customer_data.export_records':
        w.exports.push({ action_id: a.id, destination: p.destination as string, rows: p.count as number });
        return { exported: p.count as number, destination: p.destination as string };
      case 'payments.refund': {
        const pay = w.payments[p.payment_id as string]!;
        if (pay.amount_cents - pay.refunded_cents < (p.amount_cents as number)) {
          throw new EffectBlocked(`payment ${p.payment_id} no longer has ${p.amount_cents} cents refundable`);
        }
        pay.refunded_cents += p.amount_cents as number;
        return { refunded_cents: p.amount_cents as number, payment_id: p.payment_id as string };
      }
      case 'payments.transfer':
        w.transfers.push({ action_id: a.id, to_account: p.to_account as string, amount_cents: p.amount_cents as number });
        return { transferred_cents: p.amount_cents as number, to_account: p.to_account as string };
      case 'infrastructure.scale_down': {
        const s = w.services[p.service as string]!;
        if (s.replicas !== p.replicas_from) throw new EffectBlocked(`service ${p.service} no longer runs ${p.replicas_from} replicas`);
        s.replicas = p.replicas_to as number;
        return { service: p.service as string, replicas: p.replicas_to as number };
      }
      default:
        throw new EffectBlocked(`no effect for ${a.operation}`);
    }
  }

  // ---------------------------------------------------------------- replay
  replay(actionId: string): { matches: boolean; recorded: Decision; replayed: Decision } {
    const ev = [...this.audit].reverse().find((r) => r.action_id === actionId && (r.event_type === 'decision' || r.event_type === 'redecision'));
    if (!ev) throw new GateError('not_found', `no decision recorded for ${actionId}`);
    const p = ev.payload as unknown as { operation: string; params: Params; inputs: StoredContext; decision: Decision };
    const replayed = evaluate(p.operation, p.params, { ...p.inputs, policy: this.policy, policy_version: p.decision.policy_version });
    return { matches: canonicalJson(replayed as unknown as Json) === canonicalJson(p.decision as unknown as Json), recorded: p.decision, replayed };
  }
}

