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

  seed(
    key: string,
    content: string | Uint8Array,
    options: { contentType?: string; lastModified?: Date } = {},
  ): void {
    const bytes = typeof content === 'string' ? encoder.encode(content) : content;
    this.entries.set(key, { bytes, ...options });
  }

  keysFor(operation: 'head' | 'get'): string[] {
    return this.calls.filter((call) => call.op === operation).map((call) => call.key);
  }

  head(key: string): Promise<R2Object | null> {
    this.calls.push({ op: 'head', key });
    const entry = this.entries.get(key);
    return Promise.resolve(
      entry === undefined ? null : (toObject(key, entry) as unknown as R2Object),
    );
  }

  get(key: string, options?: R2GetOptions): Promise<R2ObjectBody | null> {
    this.calls.push({ op: 'get', key });
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
    return Promise.resolve(body as unknown as R2ObjectBody);
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

export function createEnv(bucket: FakeBucket, credentials: unknown): Env {
  return { BUCKET: bucket.asBinding(), CREDENTIALS: credentials } as unknown as Env;
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