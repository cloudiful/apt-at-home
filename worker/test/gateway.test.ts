import { describe, expect, it } from 'vitest';

import gateway from '../src/index.js';
import { FakeContext, authorized, createEnv, plain } from './fakes.js';
import { CLIENT, RELEASE_ID, TOKEN, createFixture } from './harness.js';

const PACKAGE_PATH = '/pool/zellij_0.45.1-1_amd64.deb';

describe('authentication gate', () => {
  it('refuses an unauthenticated request before touching storage', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(plain(PACKAGE_PATH));
    expect(response.status).toBe(401);
    expect(response.headers.get('WWW-Authenticate')).toContain('Basic realm=');
    expect(response.headers.get('Cache-Control')).toBe('no-store');
    expect(fixture.bucket.calls).toEqual([]);
  });

  it('does not reveal that an object is absent to an unauthenticated client', async () => {
    const fixture = await createFixture();
    const missing = await fixture.call(plain('/pool/not-published_1.0_all.deb'));
    const present = await fixture.call(plain(PACKAGE_PATH));
    expect(missing.status).toBe(present.status);
    expect(fixture.bucket.calls).toEqual([]);
  });

  it('refuses a wrong token and a malformed credential the same way', async () => {
    const fixture = await createFixture();
    const wrong = await fixture.call(authorized(PACKAGE_PATH, CLIENT, 'not-the-token'));
    const malformed = await fixture.call(
      plain(PACKAGE_PATH, { headers: { authorization: 'Basic !!!not-base64!!!' } }),
    );
    expect(wrong.status).toBe(401);
    expect(malformed.status).toBe(401);
    expect(fixture.bucket.calls).toEqual([]);
  });

  it('does not distinguish an unknown device from a wrong token', async () => {
    const fixture = await createFixture();
    const unknownDevice = await fixture.call(
      authorized(PACKAGE_PATH, 'unknown-device', TOKEN),
    );
    const wrongToken = await fixture.call(authorized(PACKAGE_PATH, CLIENT, 'not-the-token'));
    expect([...unknownDevice.headers]).toEqual([...wrongToken.headers]);
    expect(await unknownDevice.text()).toBe(await wrongToken.text());
  });

  it('fails closed and serves nothing when the credential secret is unusable', async () => {
    const fixture = await createFixture();
    const ctx = new FakeContext();
    for (const secret of [undefined, '', 'not json', '{}', '{"laptop":"short"}']) {
      const response = await gateway.fetch(
        authorized(PACKAGE_PATH, CLIENT, TOKEN),
        createEnv(fixture.bucket, secret),
        ctx.asBinding(),
      );
      expect(response.status).toBe(503);
      expect(response.headers.get('Cache-Control')).toBe('no-store');
    }
    expect(fixture.bucket.calls).toEqual([]);
  });
});

describe('method gate', () => {
  it.each(['POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH'])('refuses %s', async (method) => {
    const fixture = await createFixture();
    const response = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN, { method }));
    expect(response.status).toBe(405);
    expect(response.headers.get('Allow')).toBe('GET, HEAD');
    expect(fixture.bucket.calls).toEqual([]);
  });

  it('authenticates before it refuses a method', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(plain(PACKAGE_PATH, { method: 'POST' }));
    expect(response.status).toBe(401);
    expect(fixture.bucket.calls).toEqual([]);
  });
});

describe('path gate', () => {
  it.each([
    '/pool/%2e%2e%2fcurrent.json',
    '/pool/%2E%2E%2Fkey.asc',
    '/pool/zellij_0.45.1-1_amd64.deb%00',
    '/pool%2Fzellij_0.45.1-1_amd64.deb',
    '/current.json',
    '/dists/testing/Release',
    '/dists/stable/main/binary-i386/Packages',
    '/releases/20260102-030405/dists/stable/Release',
    '/etc/passwd',
  ])('refuses %s without reading the bucket', async (path) => {
    const fixture = await createFixture();
    const response = await fixture.call(authorized(path, CLIENT, TOKEN));
    expect(response.status).toBe(404);
    expect(response.headers.get('Cache-Control')).toBe('no-store');
    expect(fixture.bucket.calls).toEqual([]);
  });
});

describe('object serving', () => {
  it('serves an authenticated package file', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    expect(response.status).toBe(200);
    expect(await response.text()).toBe('debian-package-archive-bytes');
    expect(response.headers.get('Content-Type')).toBe('application/vnd.debian.binary-package');
    expect(response.headers.get('Cache-Control')).toBe('private, max-age=31536000, immutable');
    expect(fixture.bucket.keysFor('head')).toEqual(['pool/zellij_0.45.1-1_amd64.deb']);
  });

  it('serves the signing key only to an authenticated client', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(authorized('/key.asc', CLIENT, TOKEN));
    expect(response.status).toBe(200);
    expect(await response.text()).toBe('PUBLIC KEY BLOCK');
    expect(response.headers.get('Cache-Control')).toBe('private, no-cache');
  });

  it.each(['amd64', 'arm64'])(
    'serves %s package metadata from the published release',
    async (arch) => {
      const fixture = await createFixture();
      const response = await fixture.call(
        authorized(`/dists/stable/main/binary-${arch}/Packages`, CLIENT, TOKEN),
      );
      expect(response.status).toBe(200);
      expect(await response.text()).toContain(`Architecture: ${arch}`);
      expect(fixture.bucket.keysFor('head')).toEqual([
        `releases/${RELEASE_ID}/dists/stable/main/binary-${arch}/Packages`,
      ]);
    },
  );

  it('answers 404 for an authenticated request to an object that was never published', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized('/pool/not-published_1.0_all.deb', CLIENT, TOKEN),
    );
    expect(response.status).toBe(404);
    expect(response.headers.get('Cache-Control')).toBe('no-store');
  });

  it('serves a by-hash entry from the shared index without reading the pointer', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(authorized(fixture.byHashAmd64, CLIENT, TOKEN));
    expect(response.status).toBe(200);
    expect(await response.text()).toContain('Architecture: amd64');
    expect(fixture.bucket.keysFor('get')).not.toContain('current.json');
    expect(fixture.bucket.keysFor('head')).toEqual([
      `indices/amd64/SHA256/${fixture.amd64Digest}`,
    ]);
  });
});

describe('release pointer', () => {
  it('selects the published release before reading distribution metadata', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(authorized('/dists/stable/Release', CLIENT, TOKEN));
    expect(response.status).toBe(200);
    expect(fixture.bucket.keysFor('get')[0]).toBe('current.json');
    expect(fixture.bucket.keysFor('head')).toEqual([
      `releases/${RELEASE_ID}/dists/stable/Release`,
    ]);
  });

  it('answers 404 while nothing is published', async () => {
    const fixture = await createFixture();
    fixture.bucket.entries.delete('current.json');
    const response = await fixture.call(authorized('/dists/stable/Release', CLIENT, TOKEN));
    expect(response.status).toBe(404);
  });

  it.each([
    ['unreadable document', 'not json'],
    ['release id with a traversal', '{"release":"../escape"}'],
    ['release id that is not a string', '{"release":42}'],
  ])('refuses to serve through a pointer that is %s', async (_label, pointer) => {
    const fixture = await createFixture();
    fixture.bucket.seed('current.json', pointer);
    const response = await fixture.call(authorized('/dists/stable/Release', CLIENT, TOKEN));
    expect(response.status).toBe(503);
    expect(response.headers.get('Cache-Control')).toBe('no-store');
    expect(fixture.bucket.keysFor('head')).toEqual([]);
  });

  it('serves the new release and still serves the previous by-hash entry', async () => {
    const fixture = await createFixture();
    const nextRelease = '20260203-040506';
    fixture.bucket.seed(
      `releases/${nextRelease}/dists/stable/Release`,
      'Suite: stable\nVersion: 2\n',
    );
    fixture.bucket.seed('current.json', JSON.stringify({ release: nextRelease }));

    const release = await fixture.call(authorized('/dists/stable/Release', CLIENT, TOKEN));
    expect(await release.text()).toContain('Version: 2');

    const byHash = await fixture.call(authorized(fixture.byHashAmd64, CLIENT, TOKEN));
    expect(byHash.status).toBe(200);
    expect(await byHash.text()).toContain('Version: 0.45.1-1');
    expect(fixture.bucket.keysFor('head')).toContain(
      `indices/amd64/SHA256/${fixture.amd64Digest}`,
    );
  });
});

describe('no bypass route', () => {
  it.each(['/health', '/healthz', '/status', '/favicon.ico', '/robots.txt', '/key.asc'])(
    'never serves %s to an unauthenticated client',
    async (path) => {
      const fixture = await createFixture();
      const response = await fixture.call(plain(path));
      expect(response.status).toBe(401);
      expect(fixture.bucket.calls).toEqual([]);
    },
  );

  it('never serves the signing key to a client with the wrong token', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(authorized('/key.asc', CLIENT, 'wrong-token'));
    expect(response.status).toBe(401);
    expect(await response.text()).not.toContain('PUBLIC KEY');
  });
});

describe('credential hygiene', () => {
  it('never reflects a token in a response header', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    for (const [name, value] of response.headers) {
      expect(value, name).not.toContain(TOKEN);
    }
    expect(response.headers.get('Set-Cookie')).toBeNull();
    expect(response.headers.get('Location')).toBeNull();
    expect(await response.text()).not.toContain(TOKEN);
  });

  it('keeps the presented credential out of the request URL', async () => {
    const fixture = await createFixture();
    const request = authorized(PACKAGE_PATH, CLIENT, TOKEN);
    await fixture.call(request);
    expect(request.url).toBe(`https://apt.example.test${PACKAGE_PATH}`);
    expect(request.url).not.toContain(TOKEN);
  });

  it('refuses credentials that carry no device name or no token', async () => {
    const fixture = await createFixture();
    const nameless = await fixture.call(
      plain(PACKAGE_PATH, { headers: { authorization: `Basic ${btoa(':token')}` } }),
    );
    const tokenless = await fixture.call(
      plain(PACKAGE_PATH, { headers: { authorization: `Basic ${btoa(`${CLIENT}:`)}` } }),
    );
    expect(nameless.status).toBe(401);
    expect(tokenless.status).toBe(401);
  });
});