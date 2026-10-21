import { afterEach, describe, expect, it, vi } from 'vitest';

import gateway from '../src/index.js';
import { resolveAuthMode } from '../src/auth.js';
import { FakeContext, authorized, createEnv, credentialSecret, plain } from './fakes.js';
import { CLIENT, RELEASE_ID, TOKEN, createFixture } from './harness.js';

const PACKAGE_PATH = '/pool/zellij_0.45.1-1_amd64.deb';

describe('resolveAuthMode', () => {
  it('requires auth when the variable is absent or exact true', () => {
    expect(resolveAuthMode(undefined)).toEqual({ ok: true, mode: 'required' });
    expect(resolveAuthMode('true')).toEqual({ ok: true, mode: 'required' });
  });

  it('allows anonymous reads only for exact lowercase false', () => {
    expect(resolveAuthMode('false')).toEqual({ ok: true, mode: 'public' });
  });

  it.each([
    ['empty', ''],
    ['whitespace', '   '],
    ['padded false', ' false'],
    ['trailing space', 'false '],
    ['newline', 'false\n'],
    ['upper', 'FALSE'],
    ['capitalized', 'False'],
    ['true upper', 'TRUE'],
    ['true capitalized', 'True'],
    ['zero', '0'],
    ['one', '1'],
    ['null text', 'null'],
    ['undefined text', 'undefined'],
  ])('fails closed for text %s', (_label, raw) => {
    expect(resolveAuthMode(raw)).toEqual({ ok: false });
  });

  it.each([
    ['boolean true', true],
    ['boolean false', false],
    ['null', null],
    ['number', 0],
    ['object', {}],
    ['array', []],
  ])('fails closed for non-string %s', (_label, raw) => {
    expect(resolveAuthMode(raw)).toEqual({ ok: false });
  });

  it('never trims or coerces the input', () => {
    expect(resolveAuthMode(' true ')).toEqual({ ok: false });
    expect(resolveAuthMode(' false ')).toEqual({ ok: false });
  });
});

describe('public artifact mode', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('serves pool, key, and release anonymously with an absent secret', async () => {
    const fixture = await createFixture();
    const ctx = new FakeContext();
    const env = createEnv(fixture.bucket, undefined, 'false');
    for (const path of [PACKAGE_PATH, '/key.asc', '/dists/stable/Release']) {
      const response = await gateway.fetch(
        plain(path),
        env,
        ctx.asBinding(),
      );
      expect(response.status, path).toBe(200);
    }
  });

  it('ignores a wrong or malformed Authorization header in public mode', async () => {
    const fixture = await createFixture();
    const ctx = new FakeContext();
    const env = createEnv(fixture.bucket, undefined, 'false');
    const wrong = await gateway.fetch(
      authorized(PACKAGE_PATH, CLIENT, 'not-the-token'),
      env,
      ctx.asBinding(),
    );
    const malformed = await gateway.fetch(
      plain(PACKAGE_PATH, { headers: { authorization: 'Basic !!!not-base64!!!' } }),
      env,
      ctx.asBinding(),
    );
    expect(wrong.status).toBe(200);
    expect(malformed.status).toBe(200);
    expect(await wrong.text()).toBe('debian-package-archive-bytes');
  });

  it('never reads the credential secret in public mode', async () => {
    const fixture = await createFixture();
    const ctx = new FakeContext();
    let secretReads = 0;
    const env = createEnv(fixture.bucket, undefined, 'false');
    Object.defineProperty(env, 'CREDENTIALS', {
      configurable: true,
      get() {
        secretReads += 1;
        return undefined;
      },
    });
    const response = await gateway.fetch(plain(PACKAGE_PATH), env, ctx.asBinding());
    expect(response.status).toBe(200);
    expect(secretReads).toBe(0);
  });

  it('still enforces method and path after the auth mode', async () => {
    const fixture = await createFixture();
    const ctx = new FakeContext();
    const env = createEnv(fixture.bucket, undefined, 'false');
    const post = await gateway.fetch(
      plain(PACKAGE_PATH, { method: 'POST' }),
      env,
      ctx.asBinding(),
    );
    expect(post.status).toBe(405);
    const unknown = await gateway.fetch(plain('/etc/passwd'), env, ctx.asBinding());
    expect(unknown.status).toBe(404);
    expect(fixture.bucket.keysFor('head')).toEqual([]);
  });

  it('fails closed with 503 before storage for a malformed mode', async () => {
    const fixture = await createFixture();
    vi.stubGlobal('caches', { default: fixture.cache.asBinding() });
    const ctx = new FakeContext();
    for (const raw of ['', '   ', 'False', 'FALSE', true, false, null]) {
      const response = await gateway.fetch(
        plain(PACKAGE_PATH),
        createEnv(fixture.bucket, undefined, raw),
        ctx.asBinding(),
      );
      expect(response.status, String(raw)).toBe(503);
      expect(response.headers.get('Cache-Control')).toBe('no-store');
    }
    expect(fixture.bucket.calls).toEqual([]);
    expect(fixture.cache.lookups).toEqual([]);
    expect(fixture.cache.writes).toEqual([]);
  });

  it('keeps the public root available in every mode', async () => {
    const fixture = await createFixture();
    const ctx = new FakeContext();
    for (const raw of [undefined, 'true', 'false', '', 'FALSE', null]) {
      const response = await gateway.fetch(
        plain('/'),
        createEnv(fixture.bucket, undefined, raw),
        ctx.asBinding(),
      );
      expect(response.status, String(raw)).toBe(200);
    }
    expect(fixture.bucket.keysFor('get')).toEqual([
      'current.json',
      `releases/${RELEASE_ID}/manifest.json`,
    ]);
    expect(fixture.bucket.keysFor('head')).toEqual([]);
  });

  it('reinstates auth on a warmed cache when the flag returns to required', async () => {
    const fixture = await createFixture();
    vi.stubGlobal('caches', { default: fixture.cache.asBinding() });
    const ctx = new FakeContext();
    const publicEnv = createEnv(fixture.bucket, undefined, 'false');
    const first = await gateway.fetch(plain(PACKAGE_PATH), publicEnv, ctx.asBinding());
    await ctx.settled();
    expect(first.status).toBe(200);
    expect(fixture.cache.writes).toHaveLength(1);
    const lookupsAfterWarm = fixture.cache.lookups.length;
    const readsAfterWarm = fixture.bucket.calls.length;

    const requiredEnv = createEnv(
      fixture.bucket,
      await credentialSecret({ [CLIENT]: TOKEN }),
      'true',
    );
    const anonymous = await gateway.fetch(plain(PACKAGE_PATH), requiredEnv, ctx.asBinding());
    await ctx.settled();
    expect(anonymous.status).toBe(401);
    expect(fixture.cache.lookups).toHaveLength(lookupsAfterWarm);
    expect(fixture.bucket.calls).toHaveLength(readsAfterWarm);

    const authed = await gateway.fetch(
      authorized(PACKAGE_PATH, CLIENT, TOKEN),
      requiredEnv,
      ctx.asBinding(),
    );
    expect(authed.status).toBe(200);
    expect(await authed.text()).toBe('debian-package-archive-bytes');
  });
});
