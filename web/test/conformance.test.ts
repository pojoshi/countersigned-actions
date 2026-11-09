import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import fx from '../../fixtures/conformance.json';
import { actionHash, canonicalJson, chainHash, verifyRows, type Json } from '../src/canonical';
import { evaluate, type Params, type StoredContext } from '../src/engine';
import { DEFAULT_POLICY, DEFAULT_POLICY_VERSION } from '../src/policy';
import { sha256Hex } from '../src/sha256';

describe('sha256', () => {
  it('matches node:crypto on awkward inputs', () => {
    const inputs = ['', 'abc', 'é✓', 'a'.repeat(55), 'a'.repeat(56), 'a'.repeat(63), 'a'.repeat(64), 'a'.repeat(1000), '😀 astral', '\u0000nul'];
    for (const s of inputs) expect(sha256Hex(s)).toBe(createHash('sha256').update(s, 'utf8').digest('hex'));
  });
});

describe('hashing parity with Python', () => {
  it('policy version', () => expect(DEFAULT_POLICY_VERSION).toBe(fx.policy_version));
  it.each(fx.canonical)('canonical json $canonical', (c) => {
    expect(canonicalJson(c.input as Json)).toBe(c.canonical);
    expect(sha256Hex(c.canonical)).toBe(c.sha256);
  });
  it.each(fx.action_hashes)('action hash $operation', (a) => {
    expect(actionHash(a.agent, a.operation, a.params as unknown as Record<string, Json>)).toBe(a.hash);
  });
  it('audit chain', () => {
    expect(verifyRows(fx.chain as never).ok).toBe(true);
    for (const r of fx.chain) expect(chainHash(r.prev_hash, r.seq, r.ts, r.event_type, r.action_id, r.actor, r.payload as unknown as Json)).toBe(r.hash);
  });
  it('detects an edit, a deletion and a reordering', () => {
    const rows = JSON.parse(JSON.stringify(fx.chain));
    rows[1].payload = { tampered: true };
    expect(verifyRows(rows).first_bad_seq).toBe(2);
    const del = JSON.parse(JSON.stringify(fx.chain)); del.splice(1, 1);
    expect(verifyRows(del).ok).toBe(false);
    const swap = JSON.parse(JSON.stringify(fx.chain)); [swap[0], swap[1]] = [swap[1], swap[0]];
    expect(verifyRows(swap).ok).toBe(false);
  });
});

describe('decision engine parity with Python', () => {
  it.each(fx.decisions)('$name', (c) => {
    const got = evaluate(c.operation, c.params as unknown as Params, {
      ...(c.context as unknown as StoredContext), policy: DEFAULT_POLICY, policy_version: fx.policy_version,
    });
    expect(got).toEqual(c.expected);
  });
  it('the fixtures cover every outcome', () => {
    expect(new Set(fx.decisions.map((c) => c.expected.outcome))).toEqual(new Set(['execute', 'ask', 'defer', 'escalate', 'refuse']));
  });
});
