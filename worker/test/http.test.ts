import { describe, expect, it } from 'vitest';

import { authorized } from './fakes.js';
import { CLIENT, PACKAGE_BODY, TOKEN, createFixture } from './harness.js';
import type { Fixture } from './harness.js';

const PACKAGE_PATH = '/pool/zellij_0.45.1-1_amd64.deb';
const SIZE = PACKAGE_BODY.length;
const LAST_MODIFIED = 'Fri, 02 Jan 2026 03:04:05 GMT';

async function etagOf(fixture: Fixture, path: string): Promise<string> {
  const response = await fixture.call(authorized(path, CLIENT, TOKEN));
  return response.headers.get('ETag') ?? '';
}

describe('HEAD', () => {
  it('returns the headers of the matching GET and no body', async () => {
    const fixture = await createFixture();
    const get = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN));
    const head = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN, { method: 'HEAD' }));
    expect(head.status).toBe(200);
    expect([...head.headers]).toEqual([...get.headers]);
    expect(await head.text()).toBe('');
  });

  it('advertises the stored length, the validator and range support', async () => {
    const fixture = await createFixture();
    const head = await fixture.call(authorized(PACKAGE_PATH, CLIENT, TOKEN, { method: 'HEAD' }));
    expect(head.headers.get('Content-Length')).toBe(String(SIZE));
    expect(head.headers.get('Accept-Ranges')).toBe('bytes');
    expect(head.headers.get('Last-Modified')).toBe(LAST_MODIFIED);
  });
});

describe('range requests', () => {
  it('serves a bounded range with a matching Content-Range', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { range: 'bytes=0-4' } }),
    );
    expect(response.status).toBe(206);
    expect(response.headers.get('Content-Range')).toBe(`bytes 0-4/${SIZE}`);
    expect(response.headers.get('Content-Length')).toBe('5');
    expect(await response.text()).toBe(PACKAGE_BODY.slice(0, 5));
  });

  it('serves an open-ended range to the end of the object', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { range: `bytes=${SIZE - 3}-` } }),
    );
    expect(response.status).toBe(206);
    expect(response.headers.get('Content-Range')).toBe(`bytes ${SIZE - 3}-${SIZE - 1}/${SIZE}`);
    expect(await response.text()).toBe(PACKAGE_BODY.slice(-3));
  });

  it('serves a suffix range', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { range: 'bytes=-4' } }),
    );
    expect(response.status).toBe(206);
    expect(await response.text()).toBe(PACKAGE_BODY.slice(-4));
  });

  it('clamps a range that runs past the end of the object', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { range: `bytes=${SIZE - 8}-999` } }),
    );
    expect(response.status).toBe(206);
    expect(response.headers.get('Content-Range')).toBe(`bytes ${SIZE - 8}-${SIZE - 1}/${SIZE}`);
    expect(await response.text()).toBe(PACKAGE_BODY.slice(-8));
  });

  it.each([
    ['a start past the end', 'bytes=100-'],
    ['a zero length suffix', 'bytes=-0'],
    ['an inverted range', 'bytes=20-5'],
  ])('answers 416 for %s', async (_label, range) => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { range } }),
    );
    expect(response.status).toBe(416);
    expect(response.headers.get('Content-Range')).toBe(`bytes */${SIZE}`);
    expect(await response.text()).toBe('');
  });

  it.each([
    ['a multipart range', 'bytes=0-1,4-5'],
    ['an unknown unit', 'items=0-1'],
    ['a non-numeric bound', 'bytes=abc-def'],
    ['an empty specification', 'bytes='],
    ['bounds without a number', 'bytes=-'],
  ])('ignores %s and serves the whole object', async (_label, range) => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { range } }),
    );
    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Range')).toBeNull();
    expect(await response.text()).toBe(PACKAGE_BODY);
  });

  it('answers 416 for any range on an empty object', async () => {
    const fixture = await createFixture();
    fixture.bucket.seed('pool/empty_1.0_all.deb', '');
    const response = await fixture.call(
      authorized('/pool/empty_1.0_all.deb', CLIENT, TOKEN, { headers: { range: 'bytes=0-0' } }),
    );
    expect(response.status).toBe(416);
    expect(response.headers.get('Content-Range')).toBe('bytes */0');
  });

  it('applies a range to HEAD without a body', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        method: 'HEAD',
        headers: { range: 'bytes=0-4' },
      }),
    );
    expect(response.status).toBe(206);
    expect(response.headers.get('Content-Range')).toBe(`bytes 0-4/${SIZE}`);
    expect(response.headers.get('Content-Length')).toBe('5');
    expect(await response.text()).toBe('');
  });
});

describe('If-Range', () => {
  it('applies the range when the entity tag still matches', async () => {
    const fixture = await createFixture();
    const etag = await etagOf(fixture, PACKAGE_PATH);
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        headers: { range: 'bytes=0-4', 'if-range': etag },
      }),
    );
    expect(response.status).toBe(206);
  });

  it('serves the whole object when the entity tag is stale', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        headers: { range: 'bytes=0-4', 'if-range': '"0000000000000000"' },
      }),
    );
    expect(response.status).toBe(200);
    expect(await response.text()).toBe(PACKAGE_BODY);
  });

  it('never matches a weak validator, as the strong comparison requires', async () => {
    const fixture = await createFixture();
    const etag = await etagOf(fixture, PACKAGE_PATH);
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        headers: { range: 'bytes=0-4', 'if-range': `W/${etag}` },
      }),
    );
    expect(response.status).toBe(200);
  });

  it('honours an If-Range date that is not older than the stored modification time', async () => {
    const fixture = await createFixture();
    const fresh = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        headers: { range: 'bytes=0-4', 'if-range': LAST_MODIFIED },
      }),
    );
    const stale = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        headers: { range: 'bytes=0-4', 'if-range': 'Thu, 01 Jan 2026 00:00:00 GMT' },
      }),
    );
    expect(fresh.status).toBe(206);
    expect(stale.status).toBe(200);
  });

  it('applies the range when If-Range is absent', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { range: 'bytes=0-4' } }),
    );
    expect(response.status).toBe(206);
  });
});

describe('conditional requests', () => {
  it('answers 304 for a matching entity tag', async () => {
    const fixture = await createFixture();
    const etag = await etagOf(fixture, PACKAGE_PATH);
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { 'if-none-match': etag } }),
    );
    expect(response.status).toBe(304);
    expect(response.headers.get('ETag')).toBe(etag);
    expect(await response.text()).toBe('');
  });

  it('answers 304 for a wildcard entity tag', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { 'if-none-match': '*' } }),
    );
    expect(response.status).toBe(304);
  });

  it('prefers the entity tag over the modification date', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        headers: {
          'if-none-match': '"nomatch"',
          'if-modified-since': 'Sat, 01 Jan 2050 00:00:00 GMT',
        },
      }),
    );
    expect(response.status).toBe(200);
  });

  it('falls back to the modification date when no entity tag is sent', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        headers: { 'if-modified-since': 'Sat, 01 Jan 2028 00:00:00 GMT' },
      }),
    );
    expect(response.status).toBe(304);
  });

  it('ignores an unparsable modification date', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, { headers: { 'if-modified-since': 'yesterday' } }),
    );
    expect(response.status).toBe(200);
  });

  it('answers 304 to HEAD without a body', async () => {
    const fixture = await createFixture();
    const etag = await etagOf(fixture, PACKAGE_PATH);
    const response = await fixture.call(
      authorized(PACKAGE_PATH, CLIENT, TOKEN, {
        method: 'HEAD',
        headers: { 'if-none-match': etag },
      }),
    );
    expect(response.status).toBe(304);
    expect(await response.text()).toBe('');
  });
});