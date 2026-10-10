import { afterEach, describe, expect, it, vi } from 'vitest';

import gateway from '../src/index.js';
import { renderHomepageDocument } from '../src/homepage.js';
import { authorized, createEnv, FakeContext, plain } from './fakes.js';
import { CLIENT, TOKEN, createFixture } from './harness.js';

describe('public root landing page', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('serves GET / as a public HTML page without reading storage or the cache', async () => {
    const fixture = await createFixture();
    vi.stubGlobal('caches', { default: fixture.cache.asBinding() });
    const response = await fixture.call(plain('/'));

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('text/html; charset=utf-8');
    expect(response.headers.get('Cache-Control')).toBe('public, max-age=3600');
    expect(response.headers.get('Content-Security-Policy')).toContain("default-src 'none'");
    expect(response.headers.get('X-Content-Type-Options')).toBe('nosniff');
    expect(response.headers.get('Referrer-Policy')).toBe('no-referrer');
    expect(response.headers.get('Set-Cookie')).toBeNull();
    expect(response.headers.get('WWW-Authenticate')).toBeNull();

    expect(await response.text()).toContain('<title>apt-at-home</title>');
    expect(fixture.bucket.calls).toEqual([]);
    expect(fixture.cache.lookups).toEqual([]);
    expect(fixture.cache.writes).toEqual([]);
  });

  it('answers HEAD with the GET headers and no body', async () => {
    const fixture = await createFixture();
    const get = await fixture.call(plain('/'));
    const head = await fixture.call(plain('/', { method: 'HEAD' }));

    expect(head.status).toBe(200);
    expect([...head.headers]).toEqual([...get.headers]);
    expect(await head.text()).toBe('');
    expect(await get.text()).not.toBe('');
  });

  it.each(['POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH'])(
    'refuses %s at the root with an Allow header and no storage access',
    async (method) => {
      const fixture = await createFixture();
      const response = await fixture.call(plain('/', { method }));
      expect(response.status).toBe(405);
      expect(response.headers.get('Allow')).toBe('GET, HEAD');
      expect(fixture.bucket.calls).toEqual([]);
    },
  );

  it('never consults the bucket or the cache, even on a repeated request', async () => {
    const fixture = await createFixture();
    vi.stubGlobal('caches', { default: fixture.cache.asBinding() });
    await fixture.call(plain('/'));
    await fixture.call(plain('/'));
    expect(fixture.bucket.calls).toEqual([]);
    expect(fixture.cache.lookups).toEqual([]);
    expect(fixture.cache.writes).toEqual([]);
  });

  it('serves the root regardless of the presented credential', async () => {
    const fixture = await createFixture();
    const anonymous = await fixture.call(plain('/'));
    const wrong = await fixture.call(authorized('/', 'unknown-device', 'wrong-token'));
    expect(anonymous.status).toBe(200);
    expect(wrong.status).toBe(200);
    expect(fixture.bucket.calls).toEqual([]);
  });

  it('serves the root even when the credential secret is missing or unusable', async () => {
    const fixture = await createFixture();
    const ctx = new FakeContext();
    for (const secret of [undefined, '', 'not json', '{}', '{"laptop":"short"}']) {
      const response = await gateway.fetch(
        plain('/'),
        createEnv(fixture.bucket, secret),
        ctx.asBinding(),
      );
      expect(response.status).toBe(200);
      expect(response.headers.get('Content-Type')).toBe('text/html; charset=utf-8');
    }
    expect(fixture.bucket.calls).toEqual([]);
  });

  it('ignores the query string and never reflects it', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      plain('/?token=SECRET-QUERY-VALUE&device=laptop&path=/key.asc'),
    );
    const html = await response.text();
    expect(response.status).toBe(200);
    expect(html).not.toContain('SECRET-QUERY-VALUE');
    expect(html).not.toContain('device=laptop');
  });

  it('does not serve the root for any other path', async () => {
    const fixture = await createFixture();
    for (const path of ['/index.html', '/health', '/favicon.ico', '/index.htm']) {
      const response = await fixture.call(plain(path));
      expect(response.status, path).toBe(401);
    }
    expect(fixture.bucket.calls).toEqual([]);
  });

  it('keeps every secret path behind the credential gate', async () => {
    const fixture = await createFixture();
    const anonymous = await fixture.call(plain('/key.asc'));
    expect(anonymous.status).toBe(401);
    expect(anonymous.headers.get('WWW-Authenticate')).toContain('Basic realm=');

    const ctx = new FakeContext();
    const broken = await gateway.fetch(
      plain('/key.asc'),
      createEnv(fixture.bucket, undefined),
      ctx.asBinding(),
    );
    expect(broken.status).toBe(503);
    expect(fixture.bucket.calls).toEqual([]);

    const served = await fixture.call(authorized('/key.asc', CLIENT, TOKEN));
    expect(served.status).toBe(200);
    expect(await served.text()).toBe('PUBLIC KEY BLOCK');
  });
});

describe('root page content', () => {
  it('documents the support matrix, prerequisites, and ordered commands', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('zellij');
    expect(html).toContain('starship');
    expect(html).toContain('amd64');
    expect(html).toContain('arm64');
    expect(html).toContain('完整主密钥指纹');
    expect(html).toContain('sudo chmod 600 /etc/apt/auth.conf.d/apt-at-home.conf');
    expect(html).toContain('gpg --show-keys --with-fingerprint key.asc');
    expect(html).toContain('/etc/apt/keyrings/apt-at-home.gpg');
    expect(html).toContain('Signed-By');
    expect(html).toContain('sudo apt install zellij starship');
    expect(html).toContain('https://github.com/cloudiful/apt-at-home');
  });

  it('avoids token-bearing shell patterns and unauthenticated shortcuts', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();
    expect(html).not.toContain('apt-key');
    expect(html).not.toContain('trusted=yes');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('<form');

    const start = html.indexOf('curl --fail');
    expect(start).toBeGreaterThan(-1);
    const curlLine = html.slice(start).split('\n')[0] ?? '';
    expect(curlLine).toContain('--fail --show-error --user DEVICE --output key.asc');
    expect(curlLine).not.toContain('--location');
  });

  it('escapes a hostile origin instead of interpolating markup', () => {
    const html = renderHomepageDocument('https://evil"</code><script>alert(1)</script>');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
    expect(html).toContain('&quot;');
  });

  it('escapes markup-significant characters in a request origin', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(new Request('https://ho"st.test/'));
    const html = await response.text();
    expect(response.status).toBe(200);
    expect(html).not.toContain('ho"st.test');
    expect(html).toContain('ho&quot;st.test');
  });
});
