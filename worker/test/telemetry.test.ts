import { afterEach, describe, expect, it, vi } from 'vitest';

import gateway from '../src/index.js';
import { PUBLIC_STATS_SQL, classifyRequest, loadPublicStats, recordRequest } from '../src/telemetry.js';
import {
  FakeAnalyticsDataset,
  FakeAnalyticsSql,
  FakeBucket,
  FakeContext,
  FakeRateLimiter,
  createEnv,
  credentialSecret,
  plain,
} from './fakes.js';
import { CLIENT, TOKEN } from './harness.js';
import { authorized } from './fakes.js';

/** The exact event every counted request writes. */
const VISIT_POINT = { blobs: ['visit'], doubles: [1], indexes: ['visit'] };
const DOWNLOAD_POINT = { blobs: ['download'], doubles: [1], indexes: ['download'] };

/** The object key of a package file the fake bucket serves. */
const PACKAGE_KEY = 'pool/zellij_0.45.1-1_amd64.deb';

/** The request path of the same package file. */
const PACKAGE_PATH = `/${PACKAGE_KEY}`;

/** Builds an environment with the counters dataset bound. */
function envWith(dataset: FakeAnalyticsDataset, sql?: FakeAnalyticsSql) {
  return createEnv(
    new FakeBucket(),
    undefined,
    undefined,
    undefined,
    {},
    sql === undefined ? { dataset } : { dataset, sql },
  );
}

/** An HTML response, as the homepage answers. */
function htmlResponse(status = 200): Response {
  return new Response('<!doctype html>', {
    status,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}

describe('the counters dataset', () => {
  it('writes one event carrying only the kind and a count', () => {
    const dataset = new FakeAnalyticsDataset();
    recordRequest(envWith(dataset), plain('/'), htmlResponse());

    expect(dataset.points).toEqual([VISIT_POINT]);
  });

  it('never writes anything that identifies a client', () => {
    const dataset = new FakeAnalyticsDataset();
    const env = envWith(dataset);
    recordRequest(
      env,
      new Request('https://apt.example.test/pool/secret_1.0_amd64.deb', {
        headers: {
          'cf-connecting-ip': '203.0.113.7',
          authorization: `Basic ${btoa(`${CLIENT}:${TOKEN}`)}`,
          referer: 'https://example.test/private',
          'user-agent': 'private-agent',
        },
      }),
      new Response('bytes', { status: 200 }),
    );

    const payload = JSON.stringify(dataset.points);
    for (const secret of [
      '203.0.113.7',
      CLIENT,
      TOKEN,
      'secret_1.0_amd64.deb',
      'example.test',
      'private-agent',
      'referer',
    ]) {
      expect(payload, secret).not.toContain(secret);
    }
  });

  it('counts a served homepage and a served package file', () => {
    const dataset = new FakeAnalyticsDataset();
    const env = envWith(dataset);
    recordRequest(env, plain('/'), htmlResponse());
    recordRequest(env, plain('/pool/zellij_0.45.1-1_amd64.deb'), new Response('bytes'));

    expect(dataset.points).toEqual([VISIT_POINT, DOWNLOAD_POINT]);
  });

  it('counts a partial package file as a download', () => {
    const dataset = new FakeAnalyticsDataset();
    const env = envWith(dataset);
    recordRequest(env, plain('/pool/zellij_0.45.1-1_amd64.deb'), new Response('bytes', { status: 206 }));

    expect(dataset.points).toEqual([DOWNLOAD_POINT]);
  });

  it.each([
    ['a HEAD request for the homepage', plain('/', { method: 'HEAD' }), htmlResponse()],
    ['a failed homepage request', plain('/'), htmlResponse(500)],
    [
      'a browser asset',
      plain('/assets/main.js'),
      new Response('js', { status: 200, headers: { 'Content-Type': 'text/javascript' } }),
    ],
    [
      'a metadata request',
      plain('/dists/stable/main/binary-amd64/Packages'),
      new Response('index', { status: 200, headers: { 'Content-Type': 'text/plain' } }),
    ],
    ['a missing package file', plain('/pool/absent_1.0_amd64.deb'), new Response('gone', { status: 404 })],
    [
      'a not modified package file',
      plain('/pool/zellij_0.45.1-1_amd64.deb'),
      new Response(null, { status: 304 }),
    ],
    [
      'an unsatisfiable range',
      plain('/pool/zellij_0.45.1-1_amd64.deb'),
      new Response(null, { status: 416 }),
    ],
    ['a refused package file', plain('/pool/zellij_0.45.1-1_amd64.deb'), new Response('no', { status: 401 })],
    [
      'a limited package file',
      plain('/pool/zellij_0.45.1-1_amd64.deb'),
      new Response('slow down', { status: 429 }),
    ],
    [
      'an unavailable package file',
      plain('/pool/zellij_0.45.1-1_amd64.deb'),
      new Response('down', { status: 503 }),
    ],
    [
      'a path that is not a package file',
      plain('/pool/zellij_0.45.1-1_amd64.txt'),
      new Response('bytes', { status: 200 }),
    ],
  ])('does not count %s', (_label, request, response) => {
    const dataset = new FakeAnalyticsDataset();
    recordRequest(envWith(dataset), request, response);

    expect(dataset.points).toEqual([]);
  });

  it('never lets a failing dataset change the answer', () => {
    const dataset = new FakeAnalyticsDataset();
    dataset.failure = new Error('dataset refused the write');
    const env = envWith(dataset);
    const response = htmlResponse();

    expect(() => recordRequest(env, plain('/'), response)).not.toThrow();
  });

  it('works without a dataset binding at all', () => {
    const env = createEnv(new FakeBucket(), undefined);
    expect(() => recordRequest(env, plain('/'), htmlResponse())).not.toThrow();
  });
});

describe('classifying a finished request', () => {
  it('names the homepage visit and the package download', () => {
    expect(classifyRequest(plain('/'), htmlResponse())).toBe('visit');
    expect(classifyRequest(plain('/pool/zellij_0.45.1-1_amd64.deb'), new Response('b'))).toBe(
      'download',
    );
  });

  it('names nothing for every other request', () => {
    expect(classifyRequest(plain('/', { method: 'HEAD' }), htmlResponse())).toBeNull();
    expect(classifyRequest(plain('/'), new Response('x', { status: 500 }))).toBeNull();
    expect(classifyRequest(plain('/assets/main.js'), new Response('js'))).toBeNull();
    expect(classifyRequest(plain('/pool/absent.deb'), new Response('x', { status: 404 }))).toBeNull();
  });
});

describe('the counters query', () => {
  it('is the one fixed statement, with no sampling or tenancy filter', () => {
    expect(PUBLIC_STATS_SQL).toBe(
      "SELECT blob1 AS kind,SUM(double1) AS total FROM events.analyticsEngine.apt_at_home_events WHERE timestamp >= NOW() - INTERVAL '30' DAY AND blob1 IN ('visit','download') GROUP BY blob1",
    );
    for (const forbidden of [
      '_sample_interval',
      'accountTag',
      'zoneTag',
      'FORMAT',
      '${',
      '?',
    ]) {
      expect(PUBLIC_STATS_SQL, forbidden).not.toContain(forbidden);
    }
  });

  it('reads the counters through the bound dataset only', async () => {
    const sql = new FakeAnalyticsSql();
    sql.rows = [
      { kind: 'visit', total: 12 },
      { kind: 'download', total: 34 },
    ];
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    const stats = await loadPublicStats(env);

    expect(stats).toEqual({ days: 30, visits: 12, downloads: 34 });
    expect(sql.queries).toEqual([PUBLIC_STATS_SQL]);
  });

  it('reads a successful answer without rows as zero totals', async () => {
    const sql = new FakeAnalyticsSql();
    sql.rows = [];
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    expect(await loadPublicStats(env)).toEqual({ days: 30, visits: 0, downloads: 0 });
  });

  it('reads one kind without the other as a zero for the missing one', async () => {
    const sql = new FakeAnalyticsSql();
    sql.rows = [{ kind: 'visit', total: 7 }];
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    expect(await loadPublicStats(env)).toEqual({ days: 30, visits: 7, downloads: 0 });
  });

  it('rounds a fractional count to an integer', async () => {
    const sql = new FakeAnalyticsSql();
    sql.rows = [{ kind: 'download', total: 8.6 }];
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    expect(await loadPublicStats(env)).toEqual({ days: 30, visits: 0, downloads: 9 });
  });

  it.each([
    ['an unknown kind', [{ kind: 'other', total: 3 }]],
    ['a repeated kind', [{ kind: 'visit', total: 3 }, { kind: 'visit', total: 4 }]],
    ['a missing total', [{ kind: 'visit' }]],
    ['a count that is not a number', [{ kind: 'visit', total: '12' }]],
    ['a negative count', [{ kind: 'visit', total: -1 }]],
    ['a count that is not finite', [{ kind: 'visit', total: Number.POSITIVE_INFINITY }]],
    ['a count beyond the safe integers', [{ kind: 'visit', total: 1e300 }]],
    ['a row that is not an object', ['visit']],
    ['an answer without rows', null],
  ])('hides the counters for %s', async (_label, rows) => {
    const sql = new FakeAnalyticsSql();
    sql.rows = rows as unknown[];
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    expect(await loadPublicStats(env)).toBeNull();
  });

  it('hides the counters when the query fails', async () => {
    const sql = new FakeAnalyticsSql();
    sql.failure = new Error('query refused');
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    expect(await loadPublicStats(env)).toBeNull();
  });

  it('hides the counters when the binding is missing', async () => {
    const env = createEnv(new FakeBucket(), undefined);

    expect(await loadPublicStats(env)).toBeNull();
  });

  it('hides the counters when the query never answers', async () => {
    vi.useFakeTimers();
    try {
      const sql = new FakeAnalyticsSql();
      sql.stalled = true;
      const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

      const pending = loadPublicStats(env);
      await vi.advanceTimersByTimeAsync(500);

      expect(await pending).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('the cached counters', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('serves a successful answer again without a second query', async () => {
    const sql = new FakeAnalyticsSql();
    sql.rows = [{ kind: 'visit', total: 5 }];
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    const first = await loadPublicStats(env);
    const second = await loadPublicStats(env);

    expect(first).toEqual({ days: 30, visits: 5, downloads: 0 });
    expect(second).toEqual(first);
    expect(sql.queries).toHaveLength(1);
  });

  it('queries again once the successful answer is stale', async () => {
    vi.useFakeTimers();
    const sql = new FakeAnalyticsSql();
    sql.rows = [{ kind: 'visit', total: 5 }];
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    await loadPublicStats(env);
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    await loadPublicStats(env);

    expect(sql.queries).toHaveLength(2);
  });

  it('does not query again straight after a failure', async () => {
    vi.useFakeTimers();
    const sql = new FakeAnalyticsSql();
    sql.failure = new Error('query refused');
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    expect(await loadPublicStats(env)).toBeNull();
    expect(await loadPublicStats(env)).toBeNull();

    expect(sql.queries).toHaveLength(1);
  });

  it('queries again once the failure is forgotten', async () => {
    vi.useFakeTimers();
    const sql = new FakeAnalyticsSql();
    sql.failure = new Error('query refused');
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    await loadPublicStats(env);
    await vi.advanceTimersByTimeAsync(30_000);
    await loadPublicStats(env);

    expect(sql.queries).toHaveLength(2);
  });

  it('shares one query between readers that arrive together', async () => {
    const sql = new FakeAnalyticsSql();
    sql.rows = [{ kind: 'visit', total: 5 }];
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql });

    const [first, second] = await Promise.all([loadPublicStats(env), loadPublicStats(env)]);

    expect(first).toEqual(second);
    expect(sql.queries).toHaveLength(1);
  });

  it('keeps the counters of two bindings apart', async () => {
    const first = new FakeAnalyticsSql();
    first.rows = [{ kind: 'visit', total: 5 }];
    const second = new FakeAnalyticsSql();
    second.rows = [{ kind: 'visit', total: 9 }];

    const firstStats = await loadPublicStats(
      createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql: first }),
    );
    const secondStats = await loadPublicStats(
      createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { sql: second }),
    );

    expect(firstStats).toEqual({ days: 30, visits: 5, downloads: 0 });
    expect(secondStats).toEqual({ days: 30, visits: 9, downloads: 0 });
  });
});

describe('the gateway and the counters', () => {
  it('counts a homepage request and a package request', async () => {
    const dataset = new FakeAnalyticsDataset();
    const bucket = new FakeBucket();
    bucket.seed(PACKAGE_KEY, 'package bytes', { contentType: 'application/octet-stream' });
    const env = createEnv(
      bucket,
      undefined,
      'false',
      undefined,
      {},
      { dataset },
    );
    const ctx = new FakeContext();

    await gateway.fetch(plain('/'), env, ctx.asBinding());
    await gateway.fetch(plain(PACKAGE_PATH), env, ctx.asBinding());

    expect(dataset.points).toEqual([VISIT_POINT, DOWNLOAD_POINT]);
  });

  it('does not count a request the gateway refused', async () => {
    const dataset = new FakeAnalyticsDataset();
    const env = createEnv(new FakeBucket(), undefined, 'false', undefined, {}, { dataset });
    const ctx = new FakeContext();

    const response = await gateway.fetch(plain('/pool/absent_1.0_amd64.deb'), env, ctx.asBinding());

    expect(response.status).toBe(404);
    expect(dataset.points).toEqual([]);
  });

  it('does not count a request the gateway could not serve at all', async () => {
    const dataset = new FakeAnalyticsDataset();
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { dataset });
    const ctx = new FakeContext();

    const response = await gateway.fetch(plain('/pool/absent_1.0_amd64.deb'), env, ctx.asBinding());

    expect(response.status).toBe(503);
    expect(dataset.points).toEqual([]);
  });

  it('does not count a request that needed a credential it did not have', async () => {
    const dataset = new FakeAnalyticsDataset();
    const bucket = new FakeBucket();
    bucket.seed(PACKAGE_KEY, 'package bytes');
    const env = createEnv(
      bucket,
      await credentialSecret({ [CLIENT]: TOKEN }),
      'true',
      undefined,
      {},
      { dataset },
    );
    const ctx = new FakeContext();

    const response = await gateway.fetch(plain(PACKAGE_PATH), env, ctx.asBinding());

    expect(response.status).toBe(401);
    expect(dataset.points).toEqual([]);
  });

  it('does not count a request the budget refused', async () => {
    const dataset = new FakeAnalyticsDataset();
    const bucket = new FakeBucket();
    bucket.seed(PACKAGE_KEY, 'package bytes');
    const env = createEnv(
      bucket,
      undefined,
      'false',
      undefined,
      { download: new FakeRateLimiter(0) },
      { dataset },
    );
    const ctx = new FakeContext();

    const response = await gateway.fetch(
      new Request(`https://apt.example.test${PACKAGE_PATH}`, {
        headers: { 'cf-connecting-ip': '203.0.113.9' },
      }),
      env,
      ctx.asBinding(),
    );

    expect(response.status).toBe(429);
    expect(dataset.points).toEqual([]);
  });

  it('does not count a HEAD request for the homepage', async () => {
    const dataset = new FakeAnalyticsDataset();
    const env = createEnv(new FakeBucket(), undefined, undefined, undefined, {}, { dataset });
    const ctx = new FakeContext();

    const response = await gateway.fetch(plain('/', { method: 'HEAD' }), env, ctx.asBinding());

    expect(response.status).toBe(200);
    expect(dataset.points).toEqual([]);
  });

  it('counts a package request that carried a credential', async () => {
    const dataset = new FakeAnalyticsDataset();
    const bucket = new FakeBucket();
    bucket.seed(PACKAGE_KEY, 'package bytes');
    const env = createEnv(
      bucket,
      await credentialSecret({ [CLIENT]: TOKEN }),
      'true',
      undefined,
      {},
      { dataset },
    );
    const ctx = new FakeContext();

    const response = await gateway.fetch(authorized(PACKAGE_PATH, CLIENT, TOKEN), env, ctx.asBinding());

    expect(response.status).toBe(200);
    expect(dataset.points).toEqual([DOWNLOAD_POINT]);
  });
});
