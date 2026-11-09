// Canonical JSON and the audit hash chain: the same bytes as src/countersigned/canonical.py
// and audit.py. Keys are sorted by UTF-16 code unit here and by code point in Python; the
// two agree for the ASCII keys this system uses.
import { sha256Hex } from './sha256';

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export const GENESIS = '0'.repeat(64);

export function canonicalJson(v: Json): string {
  if (v === null) return 'null';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'number') {
    if (!Number.isInteger(v)) throw new Error('only integers are allowed in hashed content');
    return String(v);
  }
  if (typeof v === 'string') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canonicalJson).join(',') + ']';
  return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + canonicalJson(v[k] as Json)).join(',') + '}';
}

export const actionHash = (agent: string, operation: string, params: Record<string, Json>): string =>
  sha256Hex(canonicalJson({ agent, operation, params }));

export const policyVersion = (policy: Json): string => 'p-' + sha256Hex(canonicalJson(policy)).slice(0, 12);

export interface AuditRow {
  seq: number;
  ts: string;
  event_type: string;
  action_id: string | null;
  actor: string;
  payload: Json;
  prev_hash: string;
  hash: string;
}

export function chainHash(
  prev: string, seq: number, ts: string, eventType: string, actionId: string | null, actor: string, payload: Json,
): string {
  const body = canonicalJson({ seq, ts, event_type: eventType, action_id: actionId, actor, payload });
  return sha256Hex(prev + '\n' + body);
}

export interface VerifyResult {
  ok: boolean;
  entries: number;
  head: string;
  first_bad_seq: number | null;
  reason: string | null;
}

export function verifyRows(rows: AuditRow[]): VerifyResult {
  let prev = GENESIS;
  let n = 0;
  for (const r of rows) {
    n++;
    if (r.seq !== n) return { ok: false, entries: n - 1, head: prev, first_bad_seq: r.seq, reason: `expected seq ${n}, found ${r.seq} (entry removed or reordered)` };
    if (r.prev_hash !== prev) return { ok: false, entries: n - 1, head: prev, first_bad_seq: r.seq, reason: 'prev_hash does not match the previous entry' };
    const h = chainHash(prev, r.seq, r.ts, r.event_type, r.action_id, r.actor, r.payload);
    if (h !== r.hash) return { ok: false, entries: n - 1, head: prev, first_bad_seq: r.seq, reason: 'entry content does not match its hash (edited)' };
    prev = h;
  }
  return { ok: true, entries: n, head: prev, first_bad_seq: null, reason: null };
}
