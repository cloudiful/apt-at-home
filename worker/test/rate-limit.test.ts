import { afterEach, describe, expect, it, vi } from 'vitest';

import gateway from '../src/index.js';
import {
  FakeContext,
  FakeRateLimiter,
  authorized,
  createEnv,
  credentialSecret,
  plain,
} from './fakes.js';
import { CLIENT, TOKEN, createFixture } from './harness.js';

const PACKAGE_PATH = '/pool/zellij_0.45.1-1_amd64.deb';
const MISSING_PATH = '/pool/missing_1.0_all.deb';
const IP = '203.0.113.10';
const OTHER_IP = '198.51.100.7';

interface Limits {
  readonly index?: number;
  readonly download?: number;
  readonly package?: number;
}

interface HarnessOptions {
  readonly limits?: Limits;
  readonly omit?: { index?: boolean; download?: boolean; package?: boolean };
  readonly authRequired?: unknown;
}

function requestWithIp(path: string, ip: string | null, init: RequestInit = {}): Request {
  const headers = new Headers(init.headers);
  if (ip !== null) headers.set('cf-connecting-ip', ip);
  return authorized(path, CLIENT, TOKEN, { ...init, headers });
}

function anonymousWithIp(path: string, ip: string | null, init: RequestInit = {}): Request {
  const headers = new Headers(init.headers);
  if (ip !== null) headers.set('cf-connecting-ip', ip);
  return plain(path, { ...init, headers });
}

async function createHarness(options: HarnessOptions = {}) {
  const fixture = await createFixture();
  const limits = options.limits ?? {};
  const limiters = {
    index: new FakeRateLimiter(limits.index ?? 120),
    download: new FakeRateLimiter(limits.download ?? 30),
    package: new FakeRateLimiter(limits.package ?? 6),
  };
  const env = createEnv(
    fixture.bucket,
    await credentialSecret({ [CLIENT]: TOKEN }),
    options.authRequired,
    fixture.assets,
    {
      index: options.omit?.index === true ? null : limiters.index,
      download: options.omit?.download === true ? null : limiters.download,
      package: options.omit?.package === true ? null : limiters.package,
    },
  );
  const ctx = new FakeContext();
  return {
    fixture,
    limiters,
    env,
    ctx,
    async call(request: Request): Promise<Response> {
      const response = await gateway.fetch(request, env, ctx.asBinding());
      await ctx.settled();
      return response;
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('artifact request budgets', () => {
  it('allows six attempts on one package and throttles the seventh', async () => {
    const harness = await createHarness();
    const statuses: number[] = [];
    for (let attempt = 0; attempt < 7; attempt += 1) {
      statuses.push((await harness.call(requestWithIp(PACKAGE_PATH, IP))).status);
    }
    expect(statuses).toEqual([200, 200, 200, 200, 200, 200, 429]);
    expect(harness.limiters.package.calls).toHaveLength(7);
    expect(harness.limiters.download.calls).toHaveLength(7);
  });

  it('does not treat six requests for different files as one package budget', async () => {
    const harness = await createHarness();
    for (let index = 0; index < 6; index += 1) {
      const response = await harness.call(
        requestWithIp(`/pool/other-${index}_1.0_all.deb`, IP),
      );
      expect(response.status, String(index)).toBe(404);
    }
    const seventhFile = await harness.call(requestWithIp(PACKAGE_PATH, IP));
    expect(seventhFile.status).toBe(200);
    expect(harness.limiters.download.calls).toHaveLength(7);
    expect(new Set(harness.limiters.package.calls).size).toBe(7);
  });

  it('allows thirty package download attempts and throttles the thirty-first', async () => {
    const harness = await createHarness();
    for (let index = 0; index < 30; index += 1) {
      const response = await harness.call(
        requestWithIp(`/pool/missing-${index}_1.0_all.deb`, IP),
      );
      expect(response.status, String(index)).toBe(404);
    }
    const limited = await harness.call(
      requestWithIp('/pool/missing-30_1.0_all.deb', IP),
    );
    expect(limited.status).toBe(429);
    expect(harness.limiters.download.calls).toHaveLength(31);
    expect(harness.limiters.package.calls).toHaveLength(30);
  });

  it('allows one hundred twenty metadata attempts and throttles the next', async () => {
    const harness = await createHarness();
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const response = await harness.call(requestWithIp('/key.asc', IP));
      expect(response.status, String(attempt)).toBe(200);
    }
    const limited = await harness.call(requestWithIp('/key.asc', IP));
    expect(limited.status).toBe(429);
    expect(harness.limiters.index.calls).toHaveLength(121);
    expect(harness.limiters.download.calls).toHaveLength(0);
    expect(harness.limiters.package.calls).toHaveLength(0);
  });

  it('shares the metadata budget between the key, metadata and by-hash', async () => {
    const harness = await createHarness({ limits: { index: 1 } });
    const fixture = harness.fixture;
    expect((await harness.call(requestWithIp('/key.asc', IP))).status).toBe(200);
    expect((await harness.call(requestWithIp('/dists/stable/Release', IP))).status).toBe(429);
    expect((await harness.call(requestWithIp(fixture.byHashAmd64, IP))).status).toBe(429);
    expect(harness.limiters.index.calls).toHaveLength(3);
    expect(new Set(harness.limiters.index.calls).size).toBe(1);
  });

  it('counts an encoded alias and a query against the same canonical package bucket', async () => {
    const harness = await createHarness({ limits: { package: 1 } });
    expect((await harness.call(requestWithIp(PACKAGE_PATH, IP))).status).toBe(200);
    const alias = await harness.call(
      requestWithIp('/pool/zellij%5F0.45.1-1_amd64.deb?attempt=2', IP),
    );
    expect(alias.status).toBe(429);
    expect(harness.limiters.package.calls).toHaveLength(2);
    expect(harness.limiters.package.calls[0]).toBe(harness.limiters.package.calls[1]);
  });

  it('isolates budgets by address, file and version', async () => {
    const harness = await createHarness({ limits: { package: 1 } });
    expect((await harness.call(requestWithIp(PACKAGE_PATH, IP))).status).toBe(200);
    expect((await harness.call(requestWithIp(PACKAGE_PATH, OTHER_IP))).status).toBe(200);
    expect((await harness.call(requestWithIp(MISSING_PATH, IP))).status).toBe(404);
    expect(
      (await harness.call(requestWithIp('/pool/zellij_0.46.0-1_amd64.deb', IP))).status,
    ).toBe(404);
    expect((await harness.call(requestWithIp(PACKAGE_PATH, IP))).status).toBe(429);
    expect(harness.limiters.package.calls).toHaveLength(5);
  });

  it('trims and case-normalizes the trusted address', async () => {
    const harness = await createHarness({ limits: { index: 1 } });
    expect(
      (await harness.call(requestWithIp('/key.asc', ' 2001:DB8::1 '))).status,
    ).toBe(200);
    expect(
      (await harness.call(requestWithIp('/key.asc', '2001:db8::1'))).status,
    ).toBe(429);
    expect(harness.limiters.index.calls).toHaveLength(2);
  });
});

describe('limited response', () => {
  it('answers 429 with the advisory headers and no challenge', async () => {
    const harness = await createHarness({ limits: { package: 0 } });
    const get = await harness.call(requestWithIp(PACKAGE_PATH, IP));
    expect(get.status).toBe(429);
    expect(await get.text()).toBe('Too Many Requests\n');
    expect(get.headers.get('Retry-After')).toBe('60');
    expect(get.headers.get('Cache-Control')).toBe('no-store');
    expect(get.headers.get('Content-Type')).toBe('text/plain; charset=utf-8');
    expect(get.headers.get('X-Content-Type-Options')).toBe('nosniff');
    expect(get.headers.get('WWW-Authenticate')).toBeNull();
  });

  it('answers a limited HEAD with no body and the same metadata', async () => {
    const harness = await createHarness({ limits: { package: 0 } });
    const get = await harness.call(requestWithIp(PACKAGE_PATH, IP));
    const head = await harness.call(
      requestWithIp(PACKAGE_PATH, IP, { method: 'HEAD' }),
    );
    expect(head.status).toBe(429);
    expect(await head.text()).toBe('');
    expect([...head.headers]).toEqual([...get.headers]);
  });

  it('does not read the credential, cache or bucket for a limited request', async () => {
    const harness = await createHarness({ limits: { package: 0 } });
    vi.stubGlobal('caches', { default: harness.fixture.cache.asBinding() });
    let secretReads = 0;
    Object.defineProperty(harness.env, 'CREDENTIALS', {
      configurable: true,
      get() {
        secretReads += 1;
        return undefined;
      },
    });
    const response = await harness.call(requestWithIp(PACKAGE_PATH, IP));
    expect(response.status).toBe(429);
    expect(secretReads).toBe(0);
    expect(harness.fixture.bucket.calls).toEqual([]);
    expect(harness.fixture.cache.lookups).toEqual([]);
    expect(harness.fixture.cache.writes).toEqual([]);
  });

  it('keeps public root and browser assets outside every artifact budget', async () => {
    const harness = await createHarness({ limits: { index: 0, download: 0, package: 0 } });
    expect((await harness.call(requestWithIp('/', IP))).status).toBe(200);
    expect((await harness.call(requestWithIp('/assets/main.js', IP))).status).toBe(200);
    expect((await harness.call(requestWithIp('/key.asc', IP))).status).toBe(429);
    expect((await harness.call(requestWithIp(PACKAGE_PATH, IP))).status).toBe(429);
  });
});

describe('fail-open bindings', () => {
  it('skips a missing metadata binding instead of failing the request', async () => {
    const harness = await createHarness({ omit: { index: true } });
    expect((await harness.call(requestWithIp('/key.asc', IP))).status).toBe(200);
  });

  it('enforces a healthy per-file budget when the aggregate binding is missing', async () => {
    const harness = await createHarness({ limits: { package: 1 }, omit: { download: true } });
    expect((await harness.call(requestWithIp(PACKAGE_PATH, IP))).status).toBe(200);
    expect((await harness.call(requestWithIp(PACKAGE_PATH, IP))).status).toBe(429);
  });

  it('treats a throwing binding as unavailable and still enforces the others', async () => {
    const harness = await createHarness({ limits: { package: 0 } });
    harness.limiters.download.failure = new Error('limiter unavailable');
    const limited = await harness.call(requestWithIp(PACKAGE_PATH, IP));
    expect(limited.status).toBe(429);
    expect(harness.limiters.package.calls).toHaveLength(1);
  });

  it('never lets a failing binding bypass authentication', async () => {
    const harness = await createHarness();
    harness.limiters.download.failure = new Error('limiter unavailable');
    harness.limiters.package.failure = new Error('limiter unavailable');
    const response = await harness.call(anonymousWithIp(PACKAGE_PATH, IP));
    expect(response.status).toBe(401);
    expect(response.headers.get('Cache-Control')).toBe('no-store');
  });

  it('skips the budgets without a trusted address or with only X-Forwarded-For', async () => {
    const harness = await createHarness({ limits: { package: 0 } });
    expect((await harness.call(requestWithIp(PACKAGE_PATH, null))).status).toBe(200);
    expect(
      (
        await harness.call(
          anonymousWithIp(PACKAGE_PATH, null, {
            headers: { 'x-forwarded-for': IP, 'cf-connecting-ip': '   ' },
          }),
        )
      ).status,
    ).toBe(401);
    expect(harness.limiters.download.calls).toEqual([]);
    expect(harness.limiters.package.calls).toEqual([]);
  });
});

describe('existing behavior under a passing budget', () => {
  it('counts HEAD, conditional, range, missing and unauthenticated attempts', async () => {
    const harness = await createHarness();
    const fixture = harness.fixture;

    expect((await harness.call(requestWithIp('/key.asc', IP))).status).toBe(200);
    const etag = (
      await harness.call(requestWithIp('/key.asc', IP))
    ).headers.get('ETag');
    expect(
      (
        await harness.call(
          requestWithIp('/key.asc', IP, { headers: { 'if-none-match': etag ?? '' } }),
        )
      ).status,
    ).toBe(304);
    expect(
      (await harness.call(requestWithIp('/key.asc', IP, { method: 'HEAD' }))).status,
    ).toBe(200);

    expect(
      (
        await harness.call(
          requestWithIp(PACKAGE_PATH, IP, { headers: { range: 'bytes=0-4' } }),
        )
      ).status,
    ).toBe(206);
    expect((await harness.call(requestWithIp(MISSING_PATH, IP))).status).toBe(404);
    expect((await harness.call(anonymousWithIp(PACKAGE_PATH, IP))).status).toBe(401);

    expect(harness.limiters.index.calls).toHaveLength(4);
    expect(harness.limiters.download.calls).toHaveLength(3);
    expect(harness.limiters.package.calls).toHaveLength(3);
    expect(fixture.bucket.keysFor('head')).toContain('pool/zellij_0.45.1-1_amd64.deb');
  });

  it('preserves auth, method and path precedence for requests outside the budgets', async () => {
    const harness = await createHarness();

    const anonymous = await harness.call(anonymousWithIp(PACKAGE_PATH, IP));
    expect(anonymous.status).toBe(401);

    const post = await harness.call(
      requestWithIp(PACKAGE_PATH, IP, { method: 'POST' }),
    );
    expect(post.status).toBe(405);

    const invalid = await harness.call(requestWithIp('/etc/passwd', IP));
    expect(invalid.status).toBe(404);

    const anonymousPost = await harness.call(
      anonymousWithIp('/etc/passwd', IP, { method: 'POST' }),
    );
    expect(anonymousPost.status).toBe(401);

    expect(harness.limiters.download.calls).toHaveLength(1);
    expect(harness.limiters.package.calls).toHaveLength(1);
    expect(harness.limiters.index.calls).toHaveLength(0);
  });

  it('keeps an unusable auth mode a 503 when the budget passes', async () => {
    const harness = await createHarness({ authRequired: 'FALSE' });
    expect((await harness.call(requestWithIp(PACKAGE_PATH, IP))).status).toBe(503);
  });
});
