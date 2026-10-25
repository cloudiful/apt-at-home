/**
 * The published package catalog, read from the release metadata in the bucket.
 *
 * The root page shows what the repository actually publishes, so the catalog
 * comes from the metadata the publisher already uploaded: `current.json`
 * selects the published release and `releases/<release>/manifest.json` lists
 * the packages that release contains. The manifest is never baked into the
 * Worker, so a publish changes the page without a redeploy.
 *
 * Everything read here is untrusted storage content. Names, versions and
 * targets are validated against the shapes the publisher's own manifest
 * guarantees before they reach a command line or the page, both reads are
 * bounded in size, and the whole load is bounded in time. A reader that cannot
 * get a trustworthy catalog gets none at all rather than a stale or invented
 * one.
 *
 * A read that outlives its budget belongs to an abandoned attempt: it may not
 * write into the shared cache, so a late answer can never republish an older
 * release over a newer one.
 */
import { parsePointer } from './paths.js';
import { POINTER_KEY, isSupportedArchitecture } from './types.js';
import type { Architecture, Env } from './types.js';

/** One published package, reduced to the fields the page shows. */
export interface PackageSummary {
  readonly name: string;
  readonly version: string;
  readonly architectures: readonly Architecture[];
  /** Canonical UTC publication instant of the exact upstream release, if known. */
  readonly upstreamPublishedAt?: string;
}

/** Why a trustworthy catalog could not be produced. */
export type CatalogFailure = 'unavailable' | 'invalid' | 'empty';

export type CatalogResult =
  | {
      readonly ok: true;
      readonly releaseId: string;
      readonly packages: readonly PackageSummary[];
    }
  | { readonly ok: false; readonly reason: CatalogFailure };

/** Object key of the manifest each published release carries. */
const MANIFEST_NAME = 'manifest.json';

/** Largest pointer document that is read at all. */
const MAX_POINTER_BYTES = 4 * 1024;

/** Largest release manifest that is read at all. */
const MAX_MANIFEST_BYTES = 256 * 1024;

/** How long a successfully read catalog stays usable. */
const SUCCESS_TTL_MS = 60_000;

/** How long a failed load is remembered before it is retried. */
const FAILURE_TTL_MS = 15_000;

/**
 * Total budget for one metadata load.
 *
 * The pointer and the manifest share this one budget, so a load that is slow
 * in both reads still answers within it instead of twice over.
 */
const LOAD_TIMEOUT_MS = 3000;

/**
 * Debian package name: lowercase, digits and the punctuation Debian allows.
 * The pattern is also the shell-safe subset, so a validated name can be
 * interpolated into an install command without quoting or escaping.
 */
const PACKAGE_NAME_PATTERN = /^[a-z0-9][a-z0-9+.-]{0,62}$/;

/** Upstream version: a digit followed by the characters versions use. */
const VERSION_PATTERN = /^[0-9][A-Za-z0-9.+~:-]{0,63}$/;

/**
 * Canonical upstream publication instant: UTC RFC3339 with second precision.
 *
 * The shape alone is not enough, because a shape-valid value such as
 * `2026-02-31T00:00:00Z` names no real day. The value is parsed and required
 * to round-trip, so a normalized rollover is refused instead of published.
 */
const PUBLISHED_AT_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

/** Cached pointer for one bucket. */
interface PointerEntry {
  readonly releaseId: string;
  readonly expiresAt: number;
}

/** Cached catalog for one published release. */
interface ReleaseEntry {
  readonly packages: readonly PackageSummary[];
  readonly expiresAt: number;
}

/**
 * Everything remembered about one bucket.
 *
 * The state belongs to a single bucket binding, so two buckets never share a
 * catalog, and the release entries are keyed by release id, so a catalog is
 * only ever reused for the release it was read from.
 */
interface BucketState {
  pointer: PointerEntry | null;
  readonly releases: Map<string, ReleaseEntry>;
  failure: { readonly reason: CatalogFailure; readonly expiresAt: number } | null;
  inflight: Promise<CatalogResult> | null;
  /** The attempt that may still publish into this state. */
  current: RefreshAttempt | null;
}

/**
 * One refresh attempt against one bucket.
 *
 * Everything the attempt learns is staged here and published to the shared
 * cache only while the attempt is still the current, unexpired one. A read
 * that resolves after the deadline has passed belongs to an abandoned attempt:
 * it must never overwrite the pointer or the catalog a newer attempt published,
 * because that would serve a superseded release as if it were current.
 */
interface RefreshAttempt {
  readonly state: BucketState;
  /** Cleared as soon as the attempt is abandoned by the deadline. */
  valid: boolean;
  /** Release id this attempt read from storage. */
  pointerRead: string | null;
  /** Catalog this attempt read from storage. */
  releaseRead: {
    readonly releaseId: string;
    readonly packages: readonly PackageSummary[];
  } | null;
}

/**
 * Per-bucket cache.
 *
 * The binding object identifies the bucket for the lifetime of the isolate, so
 * the cache is keyed by it: a second bucket, in a test or in another
 * deployment, starts empty and can never read the first bucket's catalog.
 */
const BUCKET_STATES = new WeakMap<R2Bucket, BucketState>();

function stateFor(bucket: R2Bucket): BucketState {
  const existing = BUCKET_STATES.get(bucket);
  if (existing !== undefined) return existing;
  const state: BucketState = {
    pointer: null,
    releases: new Map<string, ReleaseEntry>(),
    failure: null,
    inflight: null,
    current: null,
  };
  BUCKET_STATES.set(bucket, state);
  return state;
}

/**
 * Reads the catalog of the currently published release.
 *
 * The result is cached per bucket for a minute and a failure for a quarter of
 * a minute, so a broken bucket is not hammered. Concurrent readers share one
 * load, and a reader that arrives after the cached catalog expired gets the
 * fresh one or a failure, never the expired value.
 */
export async function loadCatalog(env: Env): Promise<CatalogResult> {
  const bucket = bucketBinding(env);
  if (bucket === null) return { ok: false, reason: 'unavailable' };

  const state = stateFor(bucket);
  const now = Date.now();
  if (state.failure !== null && state.failure.expiresAt > now) {
    return { ok: false, reason: state.failure.reason };
  }
  if (state.inflight !== null) return state.inflight;

  const attempt: RefreshAttempt = {
    state,
    valid: true,
    pointerRead: null,
    releaseRead: null,
  };
  state.current = attempt;
  const inflight = refresh(attempt, bucket);
  state.inflight = inflight;
  try {
    return await inflight;
  } finally {
    if (state.inflight === inflight) {
      state.inflight = null;
      if (state.current === attempt) state.current = null;
    }
  }
}

/**
 * Loads the catalog within the read budget and remembers the outcome.
 *
 * A load that runs out of time is a failure like any other: the page falls
 * back to its short status instead of waiting for storage. The attempt is
 * abandoned at that moment, so a read that completes afterwards is ignored
 * instead of writing into the shared cache.
 */
async function refresh(attempt: RefreshAttempt, bucket: R2Bucket): Promise<CatalogResult> {
  const outcome = await withDeadline(readCatalog(attempt, bucket), LOAD_TIMEOUT_MS, () => {
    attempt.valid = false;
  });
  if (outcome === TIMED_OUT) {
    attempt.valid = false;
    publishFailure(attempt);
    return { ok: false, reason: 'unavailable' };
  }
  if (!outcome.ok) {
    attempt.valid = false;
    publishFailure(attempt, outcome.reason);
    return outcome;
  }
  publishSuccess(attempt);
  return outcome;
}

/**
 * Records a failed load.
 *
 * An expired value is never served after a failure: the pointer is dropped so
 * the next reader resolves the published release again.
 */
function publishFailure(attempt: RefreshAttempt, reason: CatalogFailure = 'unavailable'): void {
  const state = attempt.state;
  if (state.current !== attempt) return;
  state.pointer = null;
  state.failure = { reason, expiresAt: Date.now() + FAILURE_TTL_MS };
}

/**
 * Publishes what a successful attempt read.
 *
 * Only the values this attempt read itself are published, and only while the
 * attempt is still current and unexpired: an abandoned attempt must not
 * overwrite the pointer or the catalog a newer attempt published.
 */
function publishSuccess(attempt: RefreshAttempt): void {
  const state = attempt.state;
  if (!attempt.valid || state.current !== attempt) return;
  if (attempt.pointerRead !== null) {
    state.pointer = {
      releaseId: attempt.pointerRead,
      expiresAt: Date.now() + SUCCESS_TTL_MS,
    };
  }
  if (attempt.releaseRead !== null) {
    state.releases.set(attempt.releaseRead.releaseId, {
      packages: attempt.releaseRead.packages,
      expiresAt: Date.now() + SUCCESS_TTL_MS,
    });
  }
  state.failure = null;
}

/**
 * Resolves the published release and reads its catalog.
 *
 * The pointer is read whenever it is not cached, and the manifest of the
 * release it names is read whenever that release is not cached, so a repeat
 * reader costs no storage request at all. Nothing is written to the shared
 * cache here: the attempt stages what it read and the caller publishes it.
 */
async function readCatalog(attempt: RefreshAttempt, bucket: R2Bucket): Promise<CatalogResult> {
  try {
    const state = attempt.state;
    const now = Date.now();
    const pointer = state.pointer;
    let releaseId: string;
    if (pointer !== null && pointer.expiresAt > now) {
      releaseId = pointer.releaseId;
    } else {
      const resolved = await readPointer(bucket);
      if (!attempt.valid) return ABANDONED;
      if (!resolved.ok) return resolved;
      releaseId = resolved.releaseId;
      attempt.pointerRead = releaseId;
    }

    const cached = state.releases.get(releaseId);
    if (cached !== undefined && cached.expiresAt > Date.now()) {
      return { ok: true, releaseId, packages: cached.packages };
    }

    const read = await readManifest(bucket, releaseId);
    if (!attempt.valid) return ABANDONED;
    if (!read.ok) return read;
    attempt.releaseRead = { releaseId, packages: read.packages };
    return { ok: true, releaseId, packages: read.packages };
  } catch {
    return { ok: false, reason: 'unavailable' };
  }
}

/** Result of a read that belongs to an attempt the deadline already abandoned. */
const ABANDONED: CatalogResult = { ok: false, reason: 'unavailable' };

type PointerRead =
  | { readonly ok: true; readonly releaseId: string }
  | { readonly ok: false; readonly reason: CatalogFailure };

/** Reads and validates the release pointer document. */
async function readPointer(bucket: R2Bucket): Promise<PointerRead> {
  const object = await bucket.get(POINTER_KEY);
  if (object === null) return { ok: false, reason: 'unavailable' };
  if (exceedsLimit(object, MAX_POINTER_BYTES)) return { ok: false, reason: 'invalid' };
  const parsed = parsePointer(await object.text());
  if (!parsed.ok) return { ok: false, reason: 'invalid' };
  return { ok: true, releaseId: parsed.releaseId };
}

type ManifestRead =
  | { readonly ok: true; readonly packages: readonly PackageSummary[] }
  | { readonly ok: false; readonly reason: CatalogFailure };

/** Reads and validates the manifest of one published release. */
async function readManifest(
  bucket: R2Bucket,
  releaseId: string,
): Promise<ManifestRead> {
  const object = await bucket.get(`releases/${releaseId}/${MANIFEST_NAME}`);
  if (object === null) return { ok: false, reason: 'unavailable' };
  if (exceedsLimit(object, MAX_MANIFEST_BYTES)) return { ok: false, reason: 'invalid' };

  let document: unknown;
  try {
    document = JSON.parse(await object.text());
  } catch {
    return { ok: false, reason: 'invalid' };
  }

  const packages = parsePackages(document);
  if (packages === null) return { ok: false, reason: 'invalid' };
  if (packages.length === 0) return { ok: false, reason: 'empty' };
  return { ok: true, packages };
}

/** True when a stored object is larger than the reader accepts. */
function exceedsLimit(object: { readonly size: number }, limit: number): boolean {
  return typeof object.size === 'number' && object.size > limit;
}

/**
 * Validates a manifest document into the packages the page may show.
 *
 * A single unusable package invalidates the whole document: a table that
 * silently dropped rows would misstate what the repository publishes.
 */
function parsePackages(document: unknown): readonly PackageSummary[] | null {
  if (typeof document !== 'object' || document === null || Array.isArray(document)) return null;
  const entries = (document as { packages?: unknown }).packages;
  if (!Array.isArray(entries)) return null;

  const packages: PackageSummary[] = [];
  const seen = new Set<string>();
  for (const entry of entries as readonly unknown[]) {
    const summary = parsePackage(entry);
    if (summary === null) return null;
    if (seen.has(summary.name)) return null;
    seen.add(summary.name);
    packages.push(summary);
  }

  packages.sort((left, right) => (left.name < right.name ? -1 : left.name > right.name ? 1 : 0));
  return packages;
}

/** Validates one manifest package entry. */
function parsePackage(entry: unknown): PackageSummary | null {
  if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) return null;
  const record = entry as {
    name?: unknown;
    version?: unknown;
    targets?: unknown;
    upstream_published_at?: unknown;
  };

  if (typeof record.name !== 'string' || !PACKAGE_NAME_PATTERN.test(record.name)) return null;
  if (typeof record.version !== 'string' || !VERSION_PATTERN.test(record.version)) return null;
  if (
    typeof record.targets !== 'object' ||
    record.targets === null ||
    Array.isArray(record.targets)
  ) {
    return null;
  }

  const targets = record.targets as Record<string, unknown>;
  const architectures: Architecture[] = [];
  for (const key of Object.keys(targets)) {
    if (!isSupportedArchitecture(key)) return null;
    const target = targets[key];
    if (typeof target !== 'object' || target === null || Array.isArray(target)) return null;
    architectures.push(key);
  }
  if (architectures.length === 0) return null;
  architectures.sort();

  const summary: PackageSummary = {
    name: record.name,
    version: record.version,
    architectures,
  };
  // The field is optional, but a declared value must be a real canonical
  // instant: an unreadable date is a broken document, never an unknown one.
  if (record.upstream_published_at !== undefined) {
    const publishedAt = parsePublishedAt(record.upstream_published_at);
    if (publishedAt === null) return null;
    return { ...summary, upstreamPublishedAt: publishedAt };
  }
  return summary;
}

/** Returns the canonical instant, or `null` when the value is not one. */
function parsePublishedAt(value: unknown): string | null {
  if (typeof value !== 'string' || !PUBLISHED_AT_PATTERN.test(value)) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  if (parsed.toISOString().slice(0, 19) + 'Z' !== value) return null;
  return value;
}

/** The bucket binding, or null when the runtime has none. */
function bucketBinding(env: Env): R2Bucket | null {
  const binding = (env as Partial<Env> | undefined)?.BUCKET;
  if (binding === undefined || typeof binding.get !== 'function') return null;
  return binding;
}

/** Marker for a load that ran out of its time budget. */
const TIMED_OUT = Symbol('timeout');

/**
 * Races one load against a deadline.
 *
 * The work itself is not cancelled: it either finishes and fills the cache or
 * it is ignored, and the reader never waits longer than the budget. The
 * deadline callback runs as soon as the budget is over, before the abandoned
 * work can publish anything, so a late read can never write into the cache.
 */
async function withDeadline<T>(
  work: Promise<T>,
  ms: number,
  onTimeout: () => void,
): Promise<T | typeof TIMED_OUT> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expiry = new Promise<typeof TIMED_OUT>((resolve) => {
    timer = setTimeout(() => {
      onTimeout();
      resolve(TIMED_OUT);
    }, ms);
  });
  try {
    return await Promise.race([work, expiry]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}
