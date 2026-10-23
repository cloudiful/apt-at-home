/**
 * Typed in-memory doubles for the R2 binding, the runtime cache and the
 * execution context, so every test runs offline against fake storage only.
 */
import type { Env } from '../src/types.js';

const encoder = new TextEncoder();

export interface StoredEntry {
  readonly bytes: Uint8Array;
  readonly contentType?: string;
  readonly lastModified?: Date;
}

export interface BucketCall {
  readonly op: 'head' | 'get';
  readonly key: string;
}

interface FakeObject {
  readonly key: string;
  readonly size: number;
  readonly httpEtag: string;
  readonly uploaded: Date;
  readonly httpMetadata: { readonly contentType?: string } | undefined;
}

const DEFAULT_MODIFIED = new Date('2026-01-02T03:04:05Z');

/** Deterministic stand-in for the object digest R2 reports. */
function fakeEtag(bytes: Uint8Array): string {
  let hash = 0x811c9dc5;
  for (const byte of bytes) {
    hash ^= byte;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, '0').repeat(4);
}

function toObject(key: string, entry: StoredEntry): FakeObject {
  const uploaded = entry.lastModified ?? DEFAULT_MODIFIED;
  return {
    key,
    size: entry.bytes.byteLength,
    httpEtag: `"${fakeEtag(entry.bytes)}"`,
    uploaded,
    httpMetadata:
      entry.contentType === undefined ? undefined : { contentType: entry.contentType },
  };
}

function toStream(bytes: Uint8Array): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      if (bytes.byteLength > 0) controller.enqueue(bytes);
      controller.close();
    },
  });
}

export class FakeBucket {
  readonly entries = new Map<string, StoredEntry>();
  readonly calls: BucketCall[] = [];
  /** Keys whose read rejects, standing in for a failing storage request. */
  readonly failures = new Map<string, Error>();
  /** Keys whose read never answers, standing in for a stalled request. */
  readonly stalled = new Set<string>();
  /** Keys whose next read answers late, with the delay in milliseconds. */
  readonly delays = new Map<string, number>();

  seed(
    key: string,
    content: string | Uint8Array,
    options: { contentType?: string; lastModified?: Date } = {},
  ): void {
    const bytes = typeof content === 'string' ? encoder.encode(content) : content;
    this.entries.set(key, { bytes, ...options });
  }

  /** Makes every read of one key reject with the given error. */
  fail(key: string, error: Error): void {
    this.failures.set(key, error);
  }

  /** Makes every read of one key hang forever. */
  stall(key: string): void {
    this.stalled.add(key);
  }

  /**
   * Makes the next read of one key answer late.
   *
   * The stored bytes are captured when the read is requested, so a later
   * change to the bucket cannot change what the delayed read returns. That is
   * what a storage request already in flight sees, and it is what makes an
   * abandoned answer observable.
   */
  delay(key: string, ms: number): void {
    this.delays.set(key, ms);
  }

  keysFor(operation: 'head' | 'get'): string[] {
    return this.calls.filter((call) => call.op === operation).map((call) => call.key);
  }

  head(key: string): Promise<R2Object | null> {
    this.calls.push({ op: 'head', key });
    const failure = this.failures.get(key);
    if (failure !== undefined) return Promise.reject(failure);
    const entry = this.entries.get(key);
    return Promise.resolve(
      entry === undefined ? null : (toObject(key, entry) as unknown as R2Object),
    );
  }

  get(key: string, options?: R2GetOptions): Promise<R2ObjectBody | null> {
    this.calls.push({ op: 'get', key });
    const failure = this.failures.get(key);
    if (failure !== undefined) return Promise.reject(failure);
    if (this.stalled.has(key)) return new Promise<R2ObjectBody | null>(() => undefined);
    const entry = this.entries.get(key);
    if (entry === undefined) return Promise.resolve(null);
    const requested = options?.range as { offset: number; length: number } | undefined;
    const bytes = requested
      ? entry.bytes.slice(requested.offset, requested.offset + requested.length)
      : entry.bytes;
    const body = {
      ...toObject(key, entry),
      body: toStream(bytes),
      text: async () => new TextDecoder().decode(bytes),
    };
    const delay = this.delays.get(key);
    if (delay === undefined) return Promise.resolve(body as unknown as R2ObjectBody);
    this.delays.delete(key);
    return new Promise<R2ObjectBody | null>((resolve) => {
      setTimeout(() => resolve(body as unknown as R2ObjectBody), delay);
    });
  }

  asBinding(): R2Bucket {
    return this as unknown as R2Bucket;
  }
}

export interface CacheWrite {
  readonly url: string;
  readonly requestHeaders: Record<string, string>;
}

export class FakeCache {
  readonly entries = new Map<string, { headers: Record<string, string>; bytes: Uint8Array }>();
  readonly lookups: string[] = [];
  readonly writes: CacheWrite[] = [];

  async match(request: Request): Promise<Response | undefined> {
    this.lookups.push(request.url);
    const entry = this.entries.get(request.url);
    if (entry === undefined) return undefined;
    return new Response(toStream(entry.bytes), { status: 200, headers: entry.headers });
  }

  async put(request: Request, response: Response): Promise<void> {
    this.writes.push({
      url: request.url,
      requestHeaders: Object.fromEntries(request.headers.entries()),
    });
    this.entries.set(request.url, {
      headers: Object.fromEntries(response.headers.entries()),
      bytes: new Uint8Array(await response.arrayBuffer()),
    });
  }

  asBinding(): Cache {
    return this as unknown as Cache;
  }
}

export class FakeContext {
  private pending: Promise<unknown>[] = [];

  waitUntil(promise: Promise<unknown>): void {
    this.pending.push(promise.catch(() => undefined));
  }

  async settled(): Promise<void> {
    while (this.pending.length > 0) {
      await Promise.all(this.pending.splice(0));
    }
  }

  asBinding(): ExecutionContext {
    return this as unknown as ExecutionContext;
  }
}

/**
 * The public asset binding.
 *
 * The Worker asks for one flat name and the binding answers with that file
 * or with a 404, exactly as the real asset directory does.
 */
export class FakeAssets {
  readonly entries = new Map<string, { readonly body: string; readonly contentType: string }>();
  readonly requests: string[] = [];

  seed(name: string, body: string, contentType = 'application/octet-stream'): void {
    this.entries.set(name, { body, contentType });
  }

  asBinding(): Fetcher {
    const entries = this.entries;
    const requests = this.requests;
    return {
      async fetch(request: Request): Promise<Response> {
        const path = new URL(request.url).pathname.replace(/^\//, '');
        requests.push(path);
        const entry = entries.get(path);
        if (entry === undefined) return new Response('not found', { status: 404 });
        return new Response(request.method === 'HEAD' ? null : entry.body, {
          status: 200,
          headers: { 'Content-Type': entry.contentType },
        });
      },
    } as unknown as Fetcher;
  }
}

/**
 * Programmable stand-in for a first-class rate limit binding.
 *
 * Each key gets its own fixed window in the fake, so tests can assert exact
 * boundaries; the real binding is approximate and location-local.
 */
export class FakeRateLimiter {
  readonly calls: string[] = [];
  readonly counts = new Map<string, number>();
  /** Error thrown by every check while set, standing in for a failing binding. */
  failure: Error | null = null;

  constructor(readonly allowed: number = 1_000_000) {}

  async limit(options: { key: string }): Promise<{ success: boolean }> {
    this.calls.push(options.key);
    if (this.failure !== null) throw this.failure;
    const used = (this.counts.get(options.key) ?? 0) + 1;
    this.counts.set(options.key, used);
    return { success: used <= this.allowed };
  }

  keysFor(identity: string): string[] {
    return this.calls.filter((key) => key.includes(identity));
  }

  asBinding(): RateLimit {
    return this as unknown as RateLimit;
  }
}

/** Optional per-request limiter bindings; `null` omits one binding. */
export interface LimiterOverrides {
  readonly index?: FakeRateLimiter | null;
  readonly download?: FakeRateLimiter | null;
  readonly package?: FakeRateLimiter | null;
}

/** One data point written to the counters dataset. */
export interface RecordedDataPoint {
  readonly blobs?: readonly (string | ArrayBuffer | null)[];
  readonly doubles?: readonly number[];
  readonly indexes?: readonly (string | ArrayBuffer | null)[];
}

/**
 * The counters dataset binding.
 *
 * Every written data point is kept, so a test can assert the exact payload.
 * A failure stands in for a dataset that refuses the write.
 */
export class FakeAnalyticsDataset {
  readonly points: RecordedDataPoint[] = [];
  /** Error thrown by every write while it is set. */
  failure: Error | null = null;

  writeDataPoint(event?: RecordedDataPoint): void {
    if (this.failure !== null) throw this.failure;
    this.points.push(event ?? {});
  }

  asBinding(): AnalyticsEngineDataset {
    return this as unknown as AnalyticsEngineDataset;
  }
}

/**
 * The analytics SQL binding.
 *
 * The binding answers with the rows the test sets, and it records every query
 * so a test can assert the query text. A failure stands in for a query that
 * errors, and a stalled query for one that never answers.
 */
export class FakeAnalyticsSql {
  readonly queries: string[] = [];
  /** The rows the next successful query answers with. */
  rows: unknown[] = [];
  /** Error thrown by every query while it is set. */
  failure: Error | null = null;
  /** When set, a query never answers. */
  stalled = false;

  async query(request: { readonly query: string }): Promise<{ readonly data: unknown[] }> {
    this.queries.push(request.query);
    if (this.failure !== null) throw this.failure;
    if (this.stalled) return new Promise<{ readonly data: unknown[] }>(() => undefined);
    return { data: this.rows };
  }

  asBinding(): AnalyticsSQLBinding {
    return this as unknown as AnalyticsSQLBinding;
  }
}

/** Optional analytics bindings; an absent binding is left out entirely. */
export interface AnalyticsOverrides {
  readonly dataset?: FakeAnalyticsDataset;
  readonly sql?: FakeAnalyticsSql;
}

export function createEnv(
  bucket: FakeBucket,
  credentials: unknown,
  authRequired?: unknown,
  assets?: FakeAssets,
  limiters: LimiterOverrides = {},
  analytics: AnalyticsOverrides = {},
): Env {
  const env = { BUCKET: bucket.asBinding(), CREDENTIALS: credentials } as unknown as Record<
    string,
    unknown
  >;
  if (authRequired !== undefined) env['AUTH_REQUIRED'] = authRequired;
  env['ASSETS'] = (assets ?? new FakeAssets()).asBinding();
  if (limiters.index !== null) {
    env['INDEX_LIMITER'] = (limiters.index ?? new FakeRateLimiter()).asBinding();
  }
  if (limiters.download !== null) {
    env['DOWNLOAD_LIMITER'] = (limiters.download ?? new FakeRateLimiter()).asBinding();
  }
  if (limiters.package !== null) {
    env['PACKAGE_LIMITER'] = (limiters.package ?? new FakeRateLimiter()).asBinding();
  }
  if (analytics.dataset !== undefined) {
    env['ANALYTICS'] = analytics.dataset.asBinding();
  }
  if (analytics.sql !== undefined) {
    env['ANALYTICS_SQL'] = analytics.sql.asBinding();
  }
  return env as unknown as Env;
}

export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', encoder.encode(value));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

/** Builds a valid credential secret from plaintext tokens. */
export async function credentialSecret(entries: Record<string, string>): Promise<string> {
  const digests: Record<string, string> = {};
  for (const [name, token] of Object.entries(entries)) {
    digests[name] = await sha256Hex(token);
  }
  return JSON.stringify(digests);
}

export function basicHeader(name: string, token: string): string {
  return `Basic ${btoa(`${name}:${token}`)}`;
}

export function authorized(
  path: string,
  name: string,
  token: string,
  init: RequestInit = {},
): Request {
  const headers = new Headers(init.headers);
  headers.set('authorization', basicHeader(name, token));
  return new Request(`https://apt.example.test${path}`, { ...init, headers });
}

export function plain(path: string, init: RequestInit = {}): Request {
  return new Request(`https://apt.example.test${path}`, init);
}