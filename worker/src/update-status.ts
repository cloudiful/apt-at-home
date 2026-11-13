import type { UpdateStatus } from '../../web/src/lib/types';
import type { Env } from './types.js';

const states = new WeakMap<R2Bucket, { value: UpdateStatus | null; expires: number; inflight: Promise<UpdateStatus | null> | null }>();
const names = /^[a-z0-9][a-z0-9+.-]{0,62}$/;
const runUrl = /^https:\/\/github\.com\/cloudiful\/apt-at-home\/actions\/runs\/[0-9]+$/;
const instant = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const stages = ['candidate', 'discovery', 'downgrade', 'immutable'];

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function parseUpdateStatus(value: unknown, now: number): UpdateStatus | null {
  if (!object(value) || Object.keys(value).sort().join(',') !== 'checked_at,outcome,packages,run_url,schema_version') return null;
  if (value.schema_version !== 1 || typeof value.checked_at !== 'string' || !instant.test(value.checked_at)) return null;
  const time = new Date(value.checked_at);
  if (!Number.isFinite(time.getTime()) || time.getTime() > now || time.toISOString().slice(0, 19) + 'Z' !== value.checked_at) return null;
  if (typeof value.run_url !== 'string' || !runUrl.test(value.run_url)) return null;
  if (value.outcome !== 'success' && value.outcome !== 'partial' && value.outcome !== 'failure') return null;
  if (!object(value.packages) || Object.keys(value.packages).length > 4096) return null;
  const packages: Record<string, { outcome: 'checked' | 'updated' | 'deferred'; stage?: 'candidate' | 'discovery' | 'downgrade' | 'immutable' }> = Object.create(null);
  for (const [name, entry] of Object.entries(value.packages)) {
    if (!names.test(name) || !object(entry)) return null;
    if (entry.outcome === 'deferred') {
      if (Object.keys(entry).sort().join(',') !== 'outcome,stage' || typeof entry.stage !== 'string' || !stages.includes(entry.stage)) return null;
      packages[name] = { outcome: 'deferred', stage: entry.stage as 'candidate' | 'discovery' | 'downgrade' | 'immutable' };
    } else if (entry.outcome === 'checked' || entry.outcome === 'updated') {
      if (Object.keys(entry).join(',') !== 'outcome') return null;
      packages[name] = { outcome: entry.outcome };
    } else return null;
  }
  const deferred = Object.values(packages).some((entry) => entry.outcome === 'deferred');
  if ((value.outcome === 'failure' && Object.keys(packages).length !== 0) ||
      (value.outcome === 'success' && deferred) || (value.outcome === 'partial' && !deferred)) return null;
  return { checked_at: value.checked_at, run_url: value.run_url, outcome: value.outcome, packages };
}

async function read(bucket: R2Bucket): Promise<UpdateStatus | null> {
  try {
    const item = await bucket.get('status/update.json');
    if (item === null || item.size > 256 * 1024) return null;
    const text = await item.text();
    if (new TextEncoder().encode(text).byteLength > 256 * 1024) return null;
    return parseUpdateStatus(JSON.parse(text), Date.now());
  } catch {
    return null;
  }
}

export async function loadUpdateStatus(env: Env): Promise<UpdateStatus | null> {
  const bucket = (env as Partial<Env>)?.BUCKET;
  if (!bucket || typeof bucket.get !== 'function') return null;
  let state = states.get(bucket);
  if (!state) {
    state = { value: null, expires: 0, inflight: null };
    states.set(bucket, state);
  }
  if (state.expires > Date.now()) return state.value;
  if (state.inflight) return state.inflight;
  const target = state;
  const load = (async () => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), 3000); });
    try {
      const value = await Promise.race([read(bucket), timeout]);
      target.value = value;
      target.expires = Date.now() + (value ? 60_000 : 15_000);
      return value;
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  })();
  target.inflight = load;
  try { return await load; } finally { if (target.inflight === load) target.inflight = null; }
}
