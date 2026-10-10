import { afterEach, describe, expect, it, vi } from 'vitest';

import { authorized, plain } from './fakes.js';
import { CLIENT, PACKAGE_BODY, TOKEN, createFixture } from './harness.js';
import type { Fixture } from './harness.js';

const PACKAGE_PATH = '/pool/zellij_0.45.1-1_amd64.deb';
const RELEASE_PATH = '/dists/stable/Release';

async function withCache(): Promise<Fixture> {
  const fixture = await createFixture();
  vi.stubGlobal('caches', { default: fixture.cache.asBinding() });
  return fixture;
}

describe('immutable caching', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('stores an immutable object under its object key, never the request URL', async () => {
    const fixture = await withCache();
    await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    expect(fixture.cache.lookups).toEqual([`https://cache.invalid/${PACKAGE_PATH.slice(1)}`]);
    expect(fixture.cache.writes).toHaveLength(1);
    expect(fixture.cache.writes[0]?.url).toBe(`https://cache.invalid/${PACKAGE_PATH.slice(1)}`);
  });

  it('stores no credential in the cached request or its key', async () => {
    const fixture = await withCache();
    await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    const write = fixture.cache.writes[0];
    expect(write?.requestHeaders).toEqual({});
    expect(write?.url).not.toContain(TOKEN);
    expect(write?.url).not.toContain(CLIENT);
  });

  it('serves a later request from the cache without reading the bucket again', async () => {
    const fixture = await withCache();
    const first = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    const readsAfterFirst = fixture.bucket.calls.length;

    const second = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    expect(first.status).toBe(200);
    expect(second.status).toBe(200);
    expect(await second.text()).toBe(PACKAGE_BODY);
    expect(fixture.bucket.calls).toHaveLength(readsAfterFirst);
    expect(second.headers.get('Cache-Control')).toBe('private, max-age=31536000, immutable');
  });

  it('keeps the cached copy server-side private', async () => {
    const fixture = await withCache();
    await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    const cached = fixture.cache.entries.get(`https://cache.invalid/${PACKAGE_PATH.slice(1)}`);
    expect(cached?.headers['cache-control']).toBe('public, max-age=31536000');
  });

  it('never consults the cache before the request is authenticated', async () => {
    const fixture = await withCache();
    const anonymous = await fixture.call(plain(PACKAGE_PATH));
    const wrongToken = await fixture.call(authorized(PACKAGE_PATH, CLIENT, 'wrong'));
    expect(anonymous.status).toBe(401);
    expect(wrongToken.status).toBe(401);
    expect(fixture.cache.lookups).toEqual([]);
    expect(fixture.cache.writes).toEqual([]);
  });

  it('never caches a release-relative object or the signing key', async () => {
    const fixture = await withCache();
    await fixture.call(authorized(RELEASE_PATH, CLIENT, TOKEN));
    await fixture.call(authorized('/key.asc', CLIENT, TOKEN));
    expect(fixture.cache.lookups).toEqual([]);
    expect(fixture.cache.writes).toEqual([]);
  });

  it('caches a by-hash entry under its shared index key', async () => {
    const fixture = await withCache();
    await fixture.call(authorized(fixture.byHashArm64, CLIENT, TOKEN));
    expect(fixture.cache.writes[0]?.url).toBe(
      `https://cache.invalid/indices/arm64/SHA256/${fixture.arm64Digest}`,
    );
  });

  it('never caches a partial or an unsuccessful response', async () => {
    const fixture = await withCache();
    await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { range: 'bytes=0-3' } }),
    );
    await fixture.call(authorized('/pool/not-published_1.0_all.deb', CLIENT, TOKEN));
    expect(fixture.cache.writes).toEqual([]);
  });

  it('never writes an empty body for a HEAD request', async () => {
    const fixture = await withCache();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { method: 'HEAD' }),
    );
    expect(response.status).toBe(200);
    expect(fixture.cache.lookups).toEqual([]);
    expect(fixture.cache.writes).toEqual([]);
  });

  it('still serves a cached by-hash entry after the release pointer moved', async () => {
    const fixture = await withCache();
    await fixture.call(authorized(fixture.byHashAmd64, CLIENT, TOKEN));
    fixture.bucket.seed('current.json', JSON.stringify({ release: '20260203-040506' }));

    const cached = await fixture.call(authorized(fixture.byHashAmd64, CLIENT, TOKEN));
    expect(await cached.text()).toContain('Architecture: amd64');
    expect(fixture.bucket.keysFor('get')).not.toContain('current.json');
  });

  it('evaluates a warm range at the origin instead of returning the cached full body', async () => {
    const fixture = await withCache();
    const first = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    expect(first.status).toBe(200);
    const readsAfterFirst = fixture.bucket.calls.length;

    const ranged = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { range: 'bytes=0-3' } }),
    );
    expect(ranged.status).toBe(206);
    expect(ranged.headers.get('Content-Range')).not.toBeNull();
    expect(await ranged.text()).toHaveLength(4);
    expect(fixture.bucket.calls.length).toBeGreaterThan(readsAfterFirst);
  });

  it('evaluates warm conditionals at the origin instead of returning a cached 200', async () => {
    const fixture = await withCache();
    const first = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    const etag = first.headers.get('ETag') as string;
    expect(etag).not.toBeNull();

    const noneMatch = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { 'if-none-match': etag } }),
    );
    expect(noneMatch.status).toBe(304);

    const modifiedSince = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        headers: { 'if-modified-since': 'Wed, 01 Jan 2030 00:00:00 GMT' },
      }),
    );
    expect(modifiedSince.status).toBe(304);
  });
});