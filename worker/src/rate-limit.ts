/**
 * Best-effort per-IP request budgets for artifact paths.
 *
 * Throttling is abuse protection, never an authorization gate: a missing or
 * failing binding, or a request without a trusted client address, skips the
 * check and leaves the normal auth and storage flow untouched. The caller
 * classifies the path once and reaches this module before any credential,
 * pointer, cache or bucket access.
 */
import type { ClassifiedPath } from './paths.js';
import type { Env } from './types.js';

/** Header Cloudflare sets from the connection, never a client proxy chain. */
const CLIENT_IP_HEADER = 'cf-connecting-ip';

/**
 * Serializes a counter key from unambiguous parts.
 *
 * JSON quoting keeps the address and the canonical package key from colliding
 * with each other or with a delimiter chosen by either value.
 */
function counterKey(scope: string, parts: readonly string[]): string {
  return JSON.stringify([scope, ...parts]);
}

/** The trusted client address, or `null` when this request has none. */
function clientIdentity(request: Request): string | null {
  const raw = request.headers.get(CLIENT_IP_HEADER);
  if (raw === null) return null;
  const identity = raw.trim().toLowerCase();
  return identity === '' ? null : identity;
}

/** One budget check; unavailable bindings and failures always pass. */
async function hasBudget(limiter: RateLimit | undefined, key: string): Promise<boolean> {
  if (limiter === undefined || typeof limiter.limit !== 'function') return true;
  try {
    const outcome = await limiter.limit({ key });
    return outcome.success;
  } catch {
    return true;
  }
}

/**
 * Reports whether this artifact request exceeds a per-IP budget.
 *
 * Package downloads share the aggregate download budget and additionally get
 * one bucket per canonical package key. Every other valid artifact — the key,
 * distribution metadata and by-hash indexes — shares the metadata budget.
 * The aggregate is checked first, so a blocked package request does not consume
 * its per-file bucket, while a per-file denial still counts against the
 * aggregate.
 */
export async function isRateLimited(
  env: Env,
  request: Request,
  classified: ClassifiedPath,
): Promise<boolean> {
  const identity = clientIdentity(request);
  if (identity === null) return false;

  if (classified.key.startsWith('pool/')) {
    if (!(await hasBudget(env.DOWNLOAD_LIMITER, counterKey('download', [identity])))) {
      return true;
    }
    return !(await hasBudget(
      env.PACKAGE_LIMITER,
      counterKey('package', [identity, classified.key]),
    ));
  }

  return !(await hasBudget(env.INDEX_LIMITER, counterKey('metadata', [identity])));
}
