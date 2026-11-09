import { describe, expect, it } from 'vitest';
import scenarios from '../../fixtures/scenarios.json';
import { Sim, GateError } from '../src/sim';

// The Python service ran these scenarios on Postgres and recorded what happened
// (fixtures/scenarios.json). The in-memory gate must reach the same decisions and the same final states.
function run(sc: (typeof scenarios)[number]) {
  const sim = new Sim(new Date(Date.UTC(2026, 9, 26, sc.at, 0, 0)));
  if (sc.kill) sim.setKill(true, 'admin', 'scenario');
  return sc.steps.map((step) => {
    const a = sim.submit(sc.agent, step.raw);
    if ('malformed' in a) return { outcome: 'malformed', final: 'malformed', auto_ran: false };
    let autoRan = false;
    if (a.status === 'queued') {
      sim.runWorker();
      autoRan = sim.get(a.id).status === 'executed';
    } else if (step.kind === 'approved' && a.status === 'pending_approval') {
      sim.decide(a.id, 'alice', 'approve', a.action_hash);
      sim.runWorker();
    }
    return { outcome: a.decision.outcome, final: sim.get(a.id).status, auto_ran: autoRan };
  });
}

describe('scenario parity with the Python service', () => {
  it.each(scenarios)('$id $title', (sc) => {
    expect(run(sc)).toEqual(sc.expected);
  });
});

describe('in-memory gate behaviour', () => {
  const del = (segment: string, count: number) => ({ operation: 'customer_data.delete_records', params: { segment, count, ticket_id: 'TKT-1001', reason: 'x' } });

  it('records every decision in a verifiable chain, and notices an edit', () => {
    const sim = new Sim();
    const a = sim.submit('support-bot', del('eu-erasure-single', 3));
    expect('malformed' in a).toBe(false);
    sim.runWorker();
    expect(sim.verify().ok).toBe(true);
    sim.tamper(2);
    const v = sim.verify();
    expect(v.ok).toBe(false);
    expect(v.first_bad_seq).toBe(2);
  });

  it('every decision replays to the same answer', () => {
    const sim = new Sim();
    const ids = [del('eu-erasure-single', 3), del('vip', 40), del('vip', 30), del('vip', 3000)]
      .map((p) => (sim.submit('support-bot', p) as { id: string }).id);
    for (const id of ids) expect(sim.replay(id).matches).toBe(true);
  });

  it('approvals are bound to the action hash, single-use, and never self-granted', () => {
    const sim = new Sim();
    const a = sim.submit('support-bot', del('vip', 40)) as { id: string; action_hash: string };
    expect(() => sim.decide(a.id, 'support-bot', 'approve', a.action_hash)).toThrow(GateError);
    expect(() => sim.decide(a.id, 'alice', 'approve', '0'.repeat(64))).toThrow(/does not match/);
    sim.decide(a.id, 'alice', 'approve', a.action_hash);
    expect(() => sim.decide(a.id, 'bob', 'approve', a.action_hash)).toThrow(/not pending_approval/);
    expect(sim.runWorker().map((r) => r.result)).toEqual(['executed']);
    expect(sim.world.customers.vip!.live).toBe(0);
  });

  it('kill switch blocks queued work', () => {
    const sim = new Sim();
    sim.submit('support-bot', del('eu-erasure-single', 3));
    sim.setKill(true, 'admin', 'drill');
    expect(sim.runWorker()[0]).toMatchObject({ result: 'blocked' });
    expect(sim.world.customers['eu-erasure-single']!.live).toBe(3);
  });

  it('model output cannot smuggle in approval fields', () => {
    const sim = new Sim();
    const r = sim.submit('support-bot', { ...del('vip', 40), approved: true });
    expect(r).toHaveProperty('malformed');
    expect(sim.actions.length).toBe(0);
  });

  it('a deferred action is re-decided when its window opens', () => {
    const sim = new Sim();
    const a = sim.submit('infra-bot', { operation: 'infrastructure.scale_down', params: { service: 'web', replicas_from: 10, replicas_to: 9, reason: 'cost', environment: 'prod' } }) as { id: string };
    expect(sim.get(a.id).status).toBe('deferred');
    sim.setTime(new Date(Date.UTC(2026, 9, 27, 1, 5)));
    expect(sim.releaseDue()).toBe(1);
    expect(sim.runWorker()[0]).toMatchObject({ result: 'executed' });
  });
});
