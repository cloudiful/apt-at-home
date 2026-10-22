import { afterEach, describe, expect, it, vi } from 'vitest';

import { loadCatalog } from '../src/packages.js';
import { FakeBucket, createEnv } from './fakes.js';
import {
  NEXT_RELEASE_ID,
  NEXT_RELEASE_PACKAGES,
  RELEASE_ID,
  SYNTHETIC_PACKAGES,
  seedManifest,
} from './harness.js';
import type { SyntheticPackage } from './harness.js';

/** A bucket publishing one release with the given catalog. */
function publishedBucket(
  packages: readonly SyntheticPackage[] = SYNTHETIC_PACKAGES,
  releaseId: string = RELEASE_ID,
): FakeBucket {
  const bucket = new FakeBucket();
  bucket.seed('current.json', JSON.stringify({ release: releaseId }), {
    contentType: 'application/json',
  });
  seedManifest(bucket, releaseId, packages);
  return bucket;
}

/** A bucket whose pointer and manifest are seeded verbatim. */
function bucketWithDocument(pointer: string, manifest: string): FakeBucket {
  const bucket = new FakeBucket();
  bucket.seed('current.json', pointer, { contentType: 'application/json' });
  bucket.seed(`releases/${RELEASE_ID}/manifest.json`, manifest, {
    contentType: 'application/json',
  });
  return bucket;
}

describe('published package catalog', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reads the published release and returns its packages', async () => {
    const bucket = publishedBucket();
    const result = await loadCatalog(createEnv(bucket, '{}'));

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error('expected a catalog');
    expect(result.releaseId).toBe(RELEASE_ID);
    expect(result.packages.map((pkg) => pkg.name)).toEqual(['alpha', 'beta', 'gamma']);
    expect(result.packages[0]?.name).toBe('alpha');
    expect(result.packages[0]?.version).toBe('1.2.3');
    expect(result.packages[0]?.architectures).toEqual(['amd64', 'arm64']);
    expect(result.packages[1]?.architectures).toEqual(['amd64']);
    expect(result.packages[2]?.architectures).toEqual(['arm64']);
  });

  it('reads only the pointer and the manifest of the published release', async () => {
    const bucket = publishedBucket();
    await loadCatalog(createEnv(bucket, '{}'));
    expect(bucket.keysFor('get')).toEqual([
      'current.json',
      `releases/${RELEASE_ID}/manifest.json`,
    ]);
  });

  it('reuses the catalog and reads nothing for a later reader', async () => {
    const bucket = publishedBucket();
    const env = createEnv(bucket, '{}');
    await loadCatalog(env);
    await loadCatalog(env);
    expect(bucket.keysFor('get')).toHaveLength(2);
  });

  it('shares one load between concurrent readers', async () => {
    const bucket = publishedBucket();
    const env = createEnv(bucket, '{}');
    const [first, second] = await Promise.all([loadCatalog(env), loadCatalog(env)]);
    expect(first.ok).toBe(true);
    expect(second.ok).toBe(true);
    expect(bucket.keysFor('get')).toHaveLength(2);
  });

  it('picks up a newly published release once the cache expired', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-03-01T00:00:00Z'));
    const bucket = publishedBucket();
    const env = createEnv(bucket, '{}');

    const first = await loadCatalog(env);
    expect(first.ok).toBe(true);

    bucket.seed('current.json', JSON.stringify({ release: NEXT_RELEASE_ID }), {
      contentType: 'application/json',
    });
    seedManifest(bucket, NEXT_RELEASE_ID, NEXT_RELEASE_PACKAGES);
    vi.setSystemTime(new Date('2026-03-01T00:01:01Z'));

    const second = await loadCatalog(env);
    expect(second.ok).toBe(true);
    if (!second.ok) throw new Error('expected a catalog');
    expect(second.releaseId).toBe(NEXT_RELEASE_ID);
    expect(second.packages.map((pkg) => pkg.name)).toEqual(['alpha', 'delta']);
    expect(second.packages[0]?.version).toBe('2.0.0');
  });

  it('never serves an expired catalog when the refresh fails', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-03-01T00:00:00Z'));
    const bucket = publishedBucket();
    const env = createEnv(bucket, '{}');

    const first = await loadCatalog(env);
    expect(first.ok).toBe(true);

    bucket.fail('current.json', new Error('storage is down'));
    vi.setSystemTime(new Date('2026-03-01T00:01:01Z'));

    const second = await loadCatalog(env);
    expect(second.ok).toBe(false);
    if (second.ok) throw new Error('expected a failure');
    expect(second.reason).toBe('unavailable');
  });

  it('remembers a failure instead of reading storage again', async () => {
    const bucket = new FakeBucket();
    const env = createEnv(bucket, '{}');
    const first = await loadCatalog(env);
    const second = await loadCatalog(env);
    expect(first.ok).toBe(false);
    expect(second.ok).toBe(false);
    expect(bucket.keysFor('get')).toEqual(['current.json']);
  });

  it('keeps two buckets apart', async () => {
    const one = publishedBucket([{ name: 'one', version: '1.0.0', architectures: ['amd64'] }]);
    const two = publishedBucket([{ name: 'two', version: '2.0.0', architectures: ['arm64'] }]);

    const first = await loadCatalog(createEnv(one, '{}'));
    const second = await loadCatalog(createEnv(two, '{}'));

    expect(first.ok).toBe(true);
    expect(second.ok).toBe(true);
    if (!first.ok || !second.ok) throw new Error('expected a catalog');
    expect(first.packages.map((pkg) => pkg.name)).toEqual(['one']);
    expect(second.packages.map((pkg) => pkg.name)).toEqual(['two']);
  });

  it('reports a missing pointer as unavailable', async () => {
    const bucket = new FakeBucket();
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('unavailable');
  });

  it('reports a missing manifest as unavailable', async () => {
    const bucket = new FakeBucket();
    bucket.seed('current.json', JSON.stringify({ release: RELEASE_ID }), {
      contentType: 'application/json',
    });
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('unavailable');
  });

  it('reports a corrupt pointer as invalid', async () => {
    const bucket = bucketWithDocument('not json', '{}');
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('reports a corrupt manifest as invalid', async () => {
    const bucket = bucketWithDocument(JSON.stringify({ release: RELEASE_ID }), 'not json');
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('reports a manifest without packages as invalid', async () => {
    const bucket = bucketWithDocument(JSON.stringify({ release: RELEASE_ID }), '{}');
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('reports an empty package list as empty', async () => {
    const bucket = bucketWithDocument(
      JSON.stringify({ release: RELEASE_ID }),
      JSON.stringify({ packages: [] }),
    );
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('empty');
  });

  it('accepts a canonical upstream release date and keeps it', async () => {
    const bucket = publishedBucket();
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error('expected a catalog');
    const alpha = result.packages.find((pkg) => pkg.name === 'alpha');
    expect(alpha?.upstreamPublishedAt).toBe('2026-10-09T15:43:25Z');
    // A package that declares no date exposes none, never an invented one.
    const beta = result.packages.find((pkg) => pkg.name === 'beta');
    expect(beta?.upstreamPublishedAt).toBeUndefined();
  });

  it.each([
    ['an offset instead of UTC', '2026-10-09T17:43:25+02:00'],
    ['a missing seconds field', '2026-10-09T15:43Z'],
    ['a fractional second', '2026-10-09T15:43:25.000Z'],
    ['a lowercase z', '2026-10-09T15:43:25z'],
    ['an impossible calendar day', '2026-02-31T00:00:00Z'],
    ['a leap-second rollover', '2026-12-31T23:59:60Z'],
    ['an empty string', ''],
    ['a number', 1767926605],
    ['null', null],
  ])('refuses a declared upstream release date that is %s', async (_label, value) => {
    const bucket = bucketWithDocument(
      JSON.stringify({ release: RELEASE_ID }),
      JSON.stringify({
        packages: [
          { name: 'alpha', version: '1.0.0', upstream_published_at: value, targets: { amd64: {} } },
        ],
      }),
    );
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('keeps a manifest without the optional field valid', async () => {
    const bucket = bucketWithDocument(
      JSON.stringify({ release: RELEASE_ID }),
      JSON.stringify({
        packages: [{ name: 'alpha', version: '1.0.0', targets: { amd64: {} } }],
      }),
    );
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error('expected a catalog');
    expect(result.packages[0]?.upstreamPublishedAt).toBeUndefined();
  });

  it('rejects an unsupported architecture', async () => {
    const bucket = bucketWithDocument(
      JSON.stringify({ release: RELEASE_ID }),
      JSON.stringify({
        packages: [{ name: 'alpha', version: '1.0.0', targets: { riscv64: {} } }],
      }),
    );
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('rejects a package without any architecture', async () => {
    const bucket = bucketWithDocument(
      JSON.stringify({ release: RELEASE_ID }),
      JSON.stringify({ packages: [{ name: 'alpha', version: '1.0.0', targets: {} }] }),
    );
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('rejects a duplicate package name', async () => {
    const bucket = bucketWithDocument(
      JSON.stringify({ release: RELEASE_ID }),
      JSON.stringify({
        packages: [
          { name: 'alpha', version: '1.0.0', targets: { amd64: {} } },
          { name: 'alpha', version: '2.0.0', targets: { arm64: {} } },
        ],
      }),
    );
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('rejects a package name that is not shell-safe', async () => {
    const bucket = bucketWithDocument(
      JSON.stringify({ release: RELEASE_ID }),
      JSON.stringify({
        packages: [{ name: 'alpha; rm -rf /', version: '1.0.0', targets: { amd64: {} } }],
      }),
    );
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('rejects a version that is not display-safe', async () => {
    const bucket = bucketWithDocument(
      JSON.stringify({ release: RELEASE_ID }),
      JSON.stringify({
        packages: [{ name: 'alpha', version: '<b>1.0.0</b>', targets: { amd64: {} } }],
      }),
    );
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('rejects a manifest larger than the read bound', async () => {
    const bucket = bucketWithDocument(
      JSON.stringify({ release: RELEASE_ID }),
      `{"packages":[],"pad":"${'x'.repeat(300 * 1024)}"}`,
    );
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('rejects a pointer larger than the read bound', async () => {
    const bucket = new FakeBucket();
    bucket.seed('current.json', `{"release":"${'x'.repeat(8 * 1024)}"}`, {
      contentType: 'application/json',
    });
    const result = await loadCatalog(createEnv(bucket, '{}'));
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('invalid');
  });

  it('answers within the read budget when storage never replies', async () => {
    vi.useFakeTimers();
    const bucket = new FakeBucket();
    bucket.stall('current.json');
    const pending = loadCatalog(createEnv(bucket, '{}'));
    await vi.advanceTimersByTimeAsync(2000);
    const result = await pending;
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('unavailable');
  });

  it('answers unavailable when the runtime has no bucket binding', async () => {
    const env = { CREDENTIALS: '{}' } as unknown as Parameters<typeof loadCatalog>[0];
    const result = await loadCatalog(env);
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error('expected a failure');
    expect(result.reason).toBe('unavailable');
  });
});

describe('published package catalog late completions', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('ignores a pointer read that completes after its deadline', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-03-01T00:00:00Z'));
    const bucket = publishedBucket();
    const env = createEnv(bucket, '{}');
    bucket.delay('current.json', 2000);

    const pending = loadCatalog(env);
    await vi.advanceTimersByTimeAsync(1500);
    const first = await pending;
    expect(first.ok).toBe(false);
    if (first.ok) throw new Error('expected a failure');
    expect(first.reason).toBe('unavailable');

    // The publisher moves the pointer while the first read is still in flight.
    bucket.seed('current.json', JSON.stringify({ release: NEXT_RELEASE_ID }), {
      contentType: 'application/json',
    });
    seedManifest(bucket, NEXT_RELEASE_ID, NEXT_RELEASE_PACKAGES);

    vi.setSystemTime(new Date('2026-03-01T00:00:20Z'));
    const second = await loadCatalog(env);
    expect(second.ok).toBe(true);
    if (!second.ok) throw new Error('expected a catalog');
    expect(second.releaseId).toBe(NEXT_RELEASE_ID);

    // The abandoned read now answers with the release it captured.
    await vi.advanceTimersByTimeAsync(2000);

    const third = await loadCatalog(env);
    expect(third.ok).toBe(true);
    if (!third.ok) throw new Error('expected a catalog');
    expect(third.releaseId).toBe(NEXT_RELEASE_ID);
    expect(third.releaseId).not.toBe(RELEASE_ID);
  });

  it('does not seed the success cache from a read that lands in the negative window', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-03-01T00:00:00Z'));
    const bucket = publishedBucket();
    const env = createEnv(bucket, '{}');
    bucket.delay('current.json', 2000);

    const pending = loadCatalog(env);
    await vi.advanceTimersByTimeAsync(1500);
    const first = await pending;
    expect(first.ok).toBe(false);

    // The abandoned read answers while the failure is still cached.
    await vi.advanceTimersByTimeAsync(500);
    const cached = await loadCatalog(env);
    expect(cached.ok).toBe(false);
    if (cached.ok) throw new Error('expected a failure');
    expect(cached.reason).toBe('unavailable');
    expect(bucket.keysFor('get')).toEqual(['current.json']);

    // After the negative TTL the pointer is resolved from storage again.
    vi.setSystemTime(new Date('2026-03-01T00:00:20Z'));
    const third = await loadCatalog(env);
    expect(third.ok).toBe(true);
    if (!third.ok) throw new Error('expected a catalog');
    expect(third.releaseId).toBe(RELEASE_ID);
    const pointerReads = bucket.keysFor('get').filter((key) => key === 'current.json');
    expect(pointerReads).toHaveLength(2);
  });

  it('does not reuse a manifest that completed after its deadline', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-03-01T00:00:00Z'));
    const bucket = publishedBucket();
    const env = createEnv(bucket, '{}');
    const manifestKey = `releases/${RELEASE_ID}/manifest.json`;
    bucket.delay(manifestKey, 2000);

    const pending = loadCatalog(env);
    await vi.advanceTimersByTimeAsync(1500);
    const first = await pending;
    expect(first.ok).toBe(false);
    if (first.ok) throw new Error('expected a failure');
    expect(first.reason).toBe('unavailable');

    // The abandoned manifest read answers after the failure was recorded.
    await vi.advanceTimersByTimeAsync(500);

    vi.setSystemTime(new Date('2026-03-01T00:00:20Z'));
    const second = await loadCatalog(env);
    expect(second.ok).toBe(true);
    if (!second.ok) throw new Error('expected a catalog');
    expect(second.releaseId).toBe(RELEASE_ID);
    const manifestReads = bucket.keysFor('get').filter((key) => key === manifestKey);
    expect(manifestReads).toHaveLength(2);
  });
});
