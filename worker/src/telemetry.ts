/**
 * Anonymous request counters for the public landing page.
 *
 * Two kinds of request are counted: a homepage visit and a package file
 * request. Nothing else is recorded, and nothing that identifies a client is
 * ever written: an event carries only its kind and a count of one, so the
 * dataset holds request counts and no visitor data at all.
 *
 * The counters are read back with one fixed query over the same dataset. The
 * query is a constant in this file and never takes input from a request, so
 * no client can influence which table, column or filter is read.
 *
 * The reading side is defensive on purpose. A missing binding, a failed
 * query, a query that runs out of time and a malformed answer all mean the
 * same thing to the page: no counters. That is different from a successful
 * query that counted zero requests, and the two are never confused.
 */
import { classifyPath } from './paths.js';
import type { Env } from './types.js';
import type { PublicStats } from '../../web/src/lib/types';

/** The kinds of request the dataset records. */
export type AnalyticsKind = 'visit' | 'download';

/** The dataset the counters are written to and read from. */
export const DATASET_NAME = 'apt_at_home_events';

/**
 * The only query the Worker ever runs against the counters.
 *
 * The window, the dataset and the two kinds are all fixed here: the query
 * carries no parameter and reads nothing but the two counter columns. The
 * analytics SQL binding already accounts for the sampling of the dataset, so
 * the totals it returns are the weighted ones and must not be scaled again.
 */
export const PUBLIC_STATS_SQL =
  "SELECT blob1 AS kind,SUM(double1) AS total FROM events.analyticsEngine.apt_at_home_events WHERE timestamp >= NOW() - INTERVAL '30' DAY AND blob1 IN ('visit','download') GROUP BY blob1";

/** How long a homepage waits for the counters before it renders without them. */
const READ_TIMEOUT_MS = 500;

/** How long a successful answer stays usable. */
const SUCCESS_TTL_MS = 5 * 60_000;

/** How long a failed answer is remembered before it is tried again. */
const FAILURE_TTL_MS = 30_000;

/** The counters of one binding, remembered for the lifetime of the isolate. */
interface StatsCache {
  /** The last successful answer, or null when there is none. */
  value: PublicStats | null;
  /** When the successful answer stops being used. */
  valueExpiresAt: number;
  /** When a failed answer may be tried again. */
  failureExpiresAt: number;
  /** The query in flight, so concurrent readers share it. */
  inflight: Promise<PublicStats | null> | null;
}

/**
 * The cached counters, per binding.
 *
 * The binding object identifies the dataset for the lifetime of the isolate,
 * so the cache is keyed by it: a second binding — another deployment, a test
 * double — starts empty and can never read the first binding's counters. The
 * cache lives in the isolate, so it is empty again after every restart.
 */
const CACHES = new WeakMap<AnalyticsSQLBinding, StatsCache>();

function cacheFor(binding: AnalyticsSQLBinding): StatsCache {
  const existing = CACHES.get(binding);
  if (existing !== undefined) return existing;
  const cache: StatsCache = {
    value: null,
    valueExpiresAt: 0,
    failureExpiresAt: 0,
    inflight: null,
  };
  CACHES.set(binding, cache);
  return cache;
}

/**
 * Decides what kind of event a finished request is, if any.
 *
 * A visit is a `GET` of the exact root that answered with the homepage. A
 * download is a `GET` of a validated package file that answered with the
 * file. Every other request — a `HEAD`, an asset, an error, a request for
 * anything that is not a package file — is not counted, so the counters
 * describe served pages and served files and nothing else.
 */
export function classifyRequest(request: Request, response: Response): AnalyticsKind | null {
  if (request.method !== 'GET') return null;

  const url = new URL(request.url);
  if (url.pathname === '/') {
    if (response.status !== 200) return null;
    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.startsWith('text/html')) return null;
    return 'visit';
  }

  if (response.status !== 200 && response.status !== 206) return null;
  const classified = classifyPath(url.pathname);
  if (classified === null) return null;
  if (!classified.key.startsWith('pool/')) return null;
  if (!classified.key.endsWith('.deb')) return null;
  return 'download';
}

/**
 * Records one finished request in the counters.
 *
 * The event carries the kind of the request and a count of one, and nothing
 * else: no address, no path, no header, no identifier. A missing dataset
 * binding and a failing write are both ignored here, because a counter that
 * cannot be written must never change the answer the client received.
 */
export function recordRequest(env: Env, request: Request, response: Response): void {
  const kind = classifyRequest(request, response);
  if (kind === null) return;

  const dataset = env.ANALYTICS;
  if (dataset === undefined || typeof dataset.writeDataPoint !== 'function') return;

  try {
    dataset.writeDataPoint({ blobs: [kind], doubles: [1], indexes: [kind] });
  } catch {
    // A counter that cannot be written must not change the response.
  }
}

/**
 * Reads the counters of the last thirty days.
 *
 * The result is `null` when the counters could not be read at all, which is
 * not the same as a count of zero. A successful answer stays usable for five
 * minutes and a failed one for thirty seconds, so a broken binding is not
 * hammered and the page does not wait on it twice in a row. Concurrent readers
 * share one query.
 */
export async function loadPublicStats(env: Env): Promise<PublicStats | null> {
  try {
    return await readCachedStats(env);
  } catch {
    // A binding that misbehaves in any way is a missing counter, never a
    // failed page.
    return null;
  }
}

/**
 * Reads the counters, using the answer the isolate already has.
 *
 * A successful answer stays usable for five minutes and a failed one for
 * thirty seconds, so a broken binding is not hammered and the page does not
 * wait on it twice in a row. Concurrent readers share one query.
 */
async function readCachedStats(env: Env): Promise<PublicStats | null> {
  const binding = env.ANALYTICS_SQL;
  if (binding === undefined || typeof binding.query !== 'function') return null;

  const cache = cacheFor(binding);
  const now = Date.now();
  if (cache.value !== null && cache.valueExpiresAt > now) return cache.value;
  if (cache.failureExpiresAt > now) return null;
  if (cache.inflight !== null) return cache.inflight;

  const inflight = readStats(binding, cache);
  cache.inflight = inflight;
  try {
    return await inflight;
  } finally {
    if (cache.inflight === inflight) cache.inflight = null;
  }
}

/**
 * Runs the fixed query and turns its answer into the counters.
 *
 * The query is bounded: a homepage waits at most half a second for it and
 * then renders without counters. The query itself is not cancelled, but its
 * late answer is discarded and its late failure is already handled, so an
 * abandoned query never becomes an unhandled rejection and never publishes a
 * stale answer over a newer one.
 */
async function readStats(
  binding: AnalyticsSQLBinding,
  cache: StatsCache,
): Promise<PublicStats | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expiry = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), READ_TIMEOUT_MS);
  });
  const query = runQuery(binding);

  const stats = await Promise.race([query, expiry]);
  if (timer !== undefined) clearTimeout(timer);

  if (stats === null) {
    cache.value = null;
    cache.valueExpiresAt = 0;
    cache.failureExpiresAt = Date.now() + FAILURE_TTL_MS;
    return null;
  }

  cache.value = stats;
  cache.valueExpiresAt = Date.now() + SUCCESS_TTL_MS;
  cache.failureExpiresAt = 0;
  return stats;
}

/**
 * Runs the fixed query and parses its answer.
 *
 * A binding that throws instead of answering, and an answer the parser
 * refuses, are both a missing counter. The failure is handled here, so a
 * query that fails after the reader gave up is never an unhandled rejection.
 */
function runQuery(binding: AnalyticsSQLBinding): Promise<PublicStats | null> {
  try {
    return binding
      .query<{ kind?: unknown; total?: unknown }>({ query: PUBLIC_STATS_SQL })
      .then((result) => parseStats(result))
      .catch(() => null);
  } catch {
    return Promise.resolve(null);
  }
}

/**
 * Reads the counters out of the query answer.
 *
 * The answer is trusted only as far as its shape allows: a row must name a
 * known kind, must name it once, and must carry a finite, non-negative
 * count. Anything else is a malformed answer and yields no counters at all,
 * because a half-read answer would state a number the dataset never reported.
 * A successful answer without rows is a real observation of zero.
 */
function parseStats(result: { readonly data?: unknown }): PublicStats | null {
  const rows = result.data;
  if (!Array.isArray(rows)) return null;

  let visits: number | null = null;
  let downloads: number | null = null;
  for (const row of rows) {
    if (typeof row !== 'object' || row === null || Array.isArray(row)) return null;
    const record = row as { kind?: unknown; total?: unknown };
    if (record.kind !== 'visit' && record.kind !== 'download') return null;
    if (typeof record.total !== 'number' || !Number.isFinite(record.total)) return null;
    if (record.total < 0) return null;
    const total = Math.round(record.total);
    if (!Number.isSafeInteger(total)) return null;
    if (record.kind === 'visit') {
      if (visits !== null) return null;
      visits = total;
    } else {
      if (downloads !== null) return null;
      downloads = total;
    }
  }

  return { days: 30, visits: visits ?? 0, downloads: downloads ?? 0 };
}
