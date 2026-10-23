import { afterEach, describe, expect, it, vi } from 'vitest';

import gateway from '../src/index.js';
import { renderHomepageDocument } from '../src/homepage.js';
import manifest from '../dist/assets/.vite/manifest.json';
import { markTheme } from '../../web/src/components/theme-provider.js';
import { copyFor } from '../../web/src/lib/locales.js';
import { authorized, createEnv, FakeContext, plain } from './fakes.js';
import {
  CLIENT,
  RELEASE_ID,
  SYNTHETIC_PACKAGES,
  TOKEN,
  createFixture,
  seedManifest,
} from './harness.js';

const encoder = new TextEncoder();

/** The metadata keys the root page is allowed to read. */
const METADATA_KEYS = ['current.json', `releases/${RELEASE_ID}/manifest.json`];

/** The names the build gave the client entry and its stylesheet. */
const ENTRY = (manifest as unknown as Record<string, { file: string; css?: string[] }>)[
  'web/src/main.tsx'
];
const CLIENT_SCRIPT = ENTRY?.file ?? '';
const CLIENT_STYLE = ENTRY?.css?.[0] ?? '';

/** A build name carries the hash of its content. */
const HASHED_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*-[A-Za-z0-9_-]{8}\.(?:js|css)$/;

/**
 * Reads a file of this repository.
 *
 * The assertions about the theme bootstrap and the stylesheet are about their
 * text, so the text is read from the file the build and the browser use.
 */
function readSource(relative: string): string {
  const fs = (
    globalThis as unknown as {
      process: {
        getBuiltinModule(id: string): {
          readFileSync(path: URL, encoding: 'utf8'): string;
        };
      };
    }
  ).process.getBuiltinModule('fs');
  return fs.readFileSync(new URL(relative, import.meta.url), 'utf8');
}

describe('public root landing page', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('serves GET / as a public HTML page from the published catalog', async () => {
    const fixture = await createFixture();
    vi.stubGlobal('caches', { default: fixture.cache.asBinding() });
    const response = await fixture.call(plain('/'));

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('text/html; charset=utf-8');
    expect(response.headers.get('Cache-Control')).toBe('public, max-age=60');
    expect(response.headers.get('Content-Security-Policy')).toContain("default-src 'none'");
    expect(response.headers.get('X-Content-Type-Options')).toBe('nosniff');
    expect(response.headers.get('Referrer-Policy')).toBe('no-referrer');
    expect(response.headers.get('Set-Cookie')).toBeNull();
    expect(response.headers.get('WWW-Authenticate')).toBeNull();

    expect(await response.text()).toContain('<title>apt-at-home</title>');
    expect(fixture.bucket.keysFor('get')).toEqual(METADATA_KEYS);
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

  it('reuses the published catalog on a repeated request', async () => {
    const fixture = await createFixture();
    vi.stubGlobal('caches', { default: fixture.cache.asBinding() });
    await fixture.call(plain('/'));
    await fixture.call(plain('/?lang=zh'));
    expect(fixture.bucket.keysFor('get')).toEqual(METADATA_KEYS);
    expect(fixture.cache.lookups).toEqual([]);
    expect(fixture.cache.writes).toEqual([]);
  });

  it('serves the root regardless of the presented credential', async () => {
    const fixture = await createFixture();
    const anonymous = await fixture.call(plain('/'));
    const wrong = await fixture.call(authorized('/', 'unknown-device', 'wrong-token'));
    expect(anonymous.status).toBe(200);
    expect(wrong.status).toBe(200);
    expect(fixture.bucket.keysFor('get')).toEqual(METADATA_KEYS);
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
    expect(fixture.bucket.keysFor('get')).toEqual(METADATA_KEYS);
  });

  it('ignores unrelated query values and never reflects them', async () => {
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

describe('the rendered document', () => {
  it('renders the page as a React tree with the built assets', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    // The client script and its stylesheet come from the build manifest.
    expect(html).toContain(`<script src="/assets/${CLIENT_SCRIPT}" type="module"></script>`);
    expect(html).toContain(`<link rel="stylesheet" href="/assets/${CLIENT_STYLE}">`);
    // The theme bootstrap is an external script in the head, before the body.
    expect(html).toContain('<script src="/assets/theme-init.js"></script>');
    expect(html.indexOf('/assets/theme-init.js')).toBeLessThan(html.indexOf('<body>'));
    expect(html).not.toContain('<script>');
  });

  it('links the assets the build named, and every name carries its content hash', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    expect(CLIENT_SCRIPT).toMatch(HASHED_NAME);
    expect(CLIENT_STYLE).toMatch(HASHED_NAME);
    // The document never links an unhashed build file.
    expect(html).not.toContain('/assets/main.js');
    expect(html).not.toContain('/assets/main.css');
    // The theme bootstrap keeps its stable name on purpose.
    expect(html).toContain('/assets/theme-init.js');
  });

  it('keeps the title and subtitle out of the visible page', async () => {
    const fixture = await createFixture();
    for (const path of ['/', '/?lang=zh']) {
      const html = await (await fixture.call(plain(path))).text();
      expect(html, path).not.toContain('Debian 13');
      expect(html, path).not.toMatch(/<h1[^>]*>apt-at-home<\/h1>/);
      // The page still has one first-level heading for assistive technology.
      expect(html, path).toMatch(/<h1 class="sr-only">(APT repository|APT 软件源)<\/h1>/);
    }
  });

  it('offers the language, theme and repository controls', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('aria-label="Language"');
    expect(html).toContain('aria-label="Toggle theme"');
    expect(html).toContain('href="https://github.com/cloudiful/apt-at-home"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('https://img.shields.io/github/stars/cloudiful/apt-at-home?style=social');
    expect(html).toContain('apt-at-home on GitHub');
  });

  it('locks the page down to its own origin and the stars image', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(plain('/'));
    const policy = response.headers.get('Content-Security-Policy') ?? '';

    expect(policy).toContain("script-src 'self'");
    expect(policy).not.toContain("script-src 'self' 'unsafe-inline'");
    expect(policy).not.toContain('unsafe-eval');
    expect(policy).toContain("style-src 'self' 'unsafe-inline'");
    expect(policy).toContain('img-src data: https://img.shields.io');
    expect(policy).toContain("base-uri 'none'");
    expect(policy).toContain("frame-ancestors 'none'");
    for (const forbidden of ['cdn.', 'unpkg', 'jsdelivr', 'googleapis', 'fonts.']) {
      expect(policy, forbidden).not.toContain(forbidden);
    }
  });

  it('hands the browser the props as data, never as markup', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('<script id="app-props" type="application/json">');
    const payload = html.slice(
      html.indexOf('<script id="app-props" type="application/json">') +
        '<script id="app-props" type="application/json">'.length,
      html.indexOf('</script>', html.indexOf('<script id="app-props"')),
    );
    expect(payload).not.toContain('<');
    expect(JSON.parse(payload).packages).toHaveLength(SYNTHETIC_PACKAGES.length);
  });

  it('escapes a payload that would otherwise close the data block', () => {
    const html = renderHomepageDocument({
      locale: 'en',
      origin: 'https://apt.example.test',
      packages: [{ name: '</script><script>alert(1)</script>', version: '1.0.0', architectures: ['amd64'] }],
    });
    expect(html).not.toContain('</script><script>alert(1)');
    expect(html).toContain('\\u003c/script>');
  });
});

describe('root page locale negotiation', () => {
  const withHeader = (path: string, acceptLanguage: string): Request =>
    new Request(`https://apt.example.test${path}`, {
      headers: { 'accept-language': acceptLanguage },
    });

  it('serves English by default and advertises the language', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(plain('/'));
    expect(response.headers.get('Content-Language')).toBe('en');
    expect(response.headers.get('Vary')).toBe('Accept-Language');
    expect(await response.text()).toContain('<html lang="en">');
  });

  it('serves Chinese when the browser prefers it', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(withHeader('/', 'zh-CN,zh;q=0.9,en;q=0.8'));
    expect(response.headers.get('Content-Language')).toBe('zh-CN');
    expect(await response.text()).toContain('<html lang="zh-CN">');
  });

  it('lets a supported override win over the header, in both directions', async () => {
    const fixture = await createFixture();
    const zh = await fixture.call(withHeader('/?lang=zh', 'en-US,en;q=0.9'));
    expect(zh.headers.get('Content-Language')).toBe('zh-CN');
    const en = await fixture.call(withHeader('/?lang=en', 'zh-CN,zh;q=0.9'));
    expect(en.headers.get('Content-Language')).toBe('en');
  });

  it('accepts a case-insensitive override', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(plain('/?lang=EN'));
    expect(response.headers.get('Content-Language')).toBe('en');
  });

  it('ignores an unsupported override and never reflects it', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      withHeader('/?lang=%3Cscript%3Ealert(1)%3C/script%3E', 'zh-CN'),
    );
    const html = await response.text();
    expect(response.headers.get('Content-Language')).toBe('zh-CN');
    expect(html).not.toContain('<script>alert(1)');
    expect(html).not.toContain('%3Cscript');
  });

  it('marks the active locale and normalizes the switch links', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/?lang=zh'))).text();
    // The menu opens on interaction, so the document carries the trigger and
    // never a second copy of the switch.
    expect(html).toContain('aria-label="语言"');
    expect(html).not.toContain('href="?lang=en"');
    expect(html).not.toContain('href="?lang=zh"');
  });

  it('sends a Content-Length matching each locale body and differing between them', async () => {
    const fixture = await createFixture();
    const en = await fixture.call(plain('/?lang=en'));
    const zh = await fixture.call(plain('/?lang=zh'));
    const enBody = await en.text();
    const zhBody = await zh.text();
    expect(Number(en.headers.get('Content-Length'))).toBe(encoder.encode(enBody).byteLength);
    expect(Number(zh.headers.get('Content-Length'))).toBe(encoder.encode(zhBody).byteLength);
    expect(en.headers.get('Content-Length')).not.toBe(zh.headers.get('Content-Length'));
  });

  it('answers HEAD with the GET headers for each locale', async () => {
    const fixture = await createFixture();
    for (const path of ['/', '/?lang=en', '/?lang=zh']) {
      const get = await fixture.call(plain(path));
      const head = await fixture.call(plain(path, { method: 'HEAD' }));
      expect(head.status, path).toBe(200);
      expect([...head.headers], path).toEqual([...get.headers]);
      expect(await head.text(), path).toBe('');
    }
  });

  it('shares identical command blocks across locales', async () => {
    const fixture = await createFixture();
    const blocksFor = async (path: string): Promise<string[]> => {
      const html = await (await fixture.call(plain(path))).text();
      return html.match(/<pre class="code-block"><code>[\s\S]*?<\/code><\/pre>/g) ?? [];
    };
    const en = await blocksFor('/?lang=en');
    const zh = await blocksFor('/?lang=zh');
    expect(en.length).toBeGreaterThan(0);
    expect(zh).toEqual(en);
  });
});

describe('root page content', () => {
  it('renders exactly three ordered installation steps per locale', async () => {
    const fixture = await createFixture();
    for (const path of ['/', '/?lang=zh']) {
      const html = await (await fixture.call(plain(path))).text();
      const list = html.slice(html.indexOf('<ol class="steps">'), html.indexOf('</ol>'));
      expect((list.match(/<li>/g) ?? []).length, path).toBe(3);
    }
  });

  it('renders the installation steps as titles and commands without repeated intros', async () => {
    const fixture = await createFixture();
    const removed = [
      'Download the armored public key into the keyring apt trusts for this repository:',
      'Write the Deb822 source entry, signed by the key installed above:',
      'The architecture is selected automatically from the local dpkg',
      'Update the package index',
      '将 armored 公钥下载到 apt 信任的密钥环：',
      '写入 Deb822 源条目，使用上面安装的公钥签名：',
      '架构按本机 dpkg 自动选择',
      '更新软件包索引',
    ];
    const kept: Record<string, readonly string[]> = {
      '/': [
        'Install the signing key',
        'Add the repository',
        'Update and install',
        'sudo install -d -m0755 /etc/apt/keyrings',
        'sudo tee /etc/apt/sources.list.d/apt-at-home.sources',
        'sudo apt update &amp;&amp; sudo apt install alpha',
        'Only on amd64:',
      ],
      '/?lang=zh': [
        '安装签名公钥',
        '添加软件源',
        '更新并安装',
        'sudo install -d -m0755 /etc/apt/keyrings',
        'sudo tee /etc/apt/sources.list.d/apt-at-home.sources',
        'sudo apt update &amp;&amp; sudo apt install alpha',
        '仅 amd64：',
      ],
    };

    for (const [path, titles] of Object.entries(kept)) {
      const html = await (await fixture.call(plain(path))).text();
      for (const gone of removed) {
        expect(html, path).not.toContain(gone);
      }
      for (const keptText of titles) {
        expect(html, path).toContain(keptText);
      }
    }
  });

  it('keeps only the update command when the catalog is unavailable', async () => {
    const fixture = await createFixture();
    fixture.bucket.entries.delete(`releases/${RELEASE_ID}/manifest.json`);
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('sudo apt update');
    expect(html).not.toContain('Update the package index');
    expect(html).toContain('sudo install -d -m0755 /etc/apt/keyrings');
  });

  it('lists the published packages with their versions and architectures', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('Published packages');
    expect(html).toContain('Package');
    expect(html).toContain('Upstream version');
    expect(html).toContain('Architecture');
    expect(html).toContain('alpha');
    expect(html).toContain('1.2.3');
    expect(html).toContain('amd64, arm64');
    expect(html).toContain('0.4.0');
    expect(html).toContain('2.0.0');
  });

  it('labels the table for assistive technology without repeating it visibly', async () => {
    const fixture = await createFixture();
    for (const [path, caption] of [
      ['/', 'Published packages'],
      ['/?lang=zh', '已发布的软件包'],
    ] as const) {
      const html = await (await fixture.call(plain(path))).text();
      const label = html.slice(html.indexOf('<caption'), html.indexOf('</caption>'));
      expect(label, path).toContain(caption);
      expect(label, path).toContain('sr-only');
      expect(html, path).not.toContain('<caption>');
      expect(html, path).not.toContain('display: none');
    }
  });

  it('builds the install commands from the same published catalog', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('sudo apt update &amp;&amp; sudo apt install alpha');
    expect(html).toContain('Only on amd64:');
    expect(html).toContain('sudo apt install beta');
    expect(html).toContain('Only on arm64:');
    expect(html).toContain('sudo apt install gamma');
    expect(html).not.toContain('sudo apt install alpha beta');
  });

  it('shows the short status and no table when the catalog cannot be read', async () => {
    const fixture = await createFixture();
    fixture.bucket.entries.delete(`releases/${RELEASE_ID}/manifest.json`);
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('role="status"');
    expect(html).toContain('The published package list is unavailable right now.');
    expect(html).not.toContain('<table');
    expect(html).not.toContain('sudo apt install');
    expect(html).toContain('sudo apt update');
    expect(html).toContain('/etc/apt/keyrings/apt-at-home.asc');
  });

  it('never renders an untrusted manifest value', async () => {
    const fixture = await createFixture();
    fixture.bucket.seed(
      `releases/${RELEASE_ID}/manifest.json`,
      JSON.stringify({
        packages: [
          {
            name: '<script>alert(1)</script>',
            version: '1.0.0',
            targets: { amd64: {} },
          },
        ],
      }),
      { contentType: 'application/json' },
    );
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).not.toContain('<script>alert(1)');
    expect(html).not.toContain('alert(1)');
    expect(html).toContain('The published package list is unavailable right now.');
    expect(html).not.toContain('<table');
  });

  it('picks up a newly published package without a template change', async () => {
    const fixture = await createFixture();
    seedManifest(fixture.bucket, RELEASE_ID, [
      ...SYNTHETIC_PACKAGES,
      { name: 'delta', version: '3.1.4', architectures: ['amd64', 'arm64'] },
    ]);
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('delta');
    expect(html).toContain('3.1.4');
    expect(html).toContain('sudo apt update &amp;&amp; sudo apt install alpha delta');
  });

  it('serves an anonymous key download without unsafe shortcuts', async () => {
    const fixture = await createFixture();
    for (const path of ['/?lang=en', '/?lang=zh']) {
      const html = await (await fixture.call(plain(path))).text();
      expect(html, path).not.toContain('apt-key');
      expect(html, path).not.toContain('trusted=yes');
      expect(html, path).not.toContain('--dearmor');
      expect(html, path).not.toContain('apt-at-home.gpg');
      expect(html, path).not.toContain('<form');

      const start = html.indexOf('curl --fail');
      expect(start, path).toBeGreaterThan(-1);
      const curlLine = html.slice(start).split('\n')[0] ?? '';
      expect(curlLine, path).toContain(
        '--fail --show-error --output /etc/apt/keyrings/apt-at-home.asc',
      );
      expect(curlLine, path).not.toContain('--location');
      expect(curlLine, path).not.toContain('--user');
    }
  });

  it('keeps the fingerprint and device-credential guidance collapsed by default', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/?lang=en'))).text();
    expect(html.match(/<details class="more">/g) ?? []).toHaveLength(2);
    expect(html).not.toContain('<details open');
    expect(html).toContain('gpg --show-keys /etc/apt/keyrings/apt-at-home.asc');
    expect(html).toContain('/etc/apt/auth.conf.d/apt-at-home.conf');
    expect(html).toContain('machine https://apt.example.test login DEVICE password TOKEN');
  });

  it('escapes a hostile origin instead of interpolating markup', () => {
    for (const locale of ['en', 'zh'] as const) {
      const html = renderHomepageDocument({
        locale,
        origin: 'https://evil"</code><script>alert(1)</script>',
        packages: null,
      });
      expect(html, locale).not.toContain('<script>alert(1)');
      expect(html, locale).toContain('&lt;script&gt;');
      expect(html, locale).toContain('&quot;');
    }
  });

  it('escapes markup-significant characters in a request origin', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(new Request('https://ho"st.test/'));
    const html = await response.text();
    expect(response.status).toBe(200);
    expect(html).not.toContain('ho"st.test');
    expect(html).toContain('ho&quot;st.test');
  });

  it('shows the upstream release date as a UTC date and an unknown as an em dash', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/?lang=en'))).text();

    expect(html).toContain('Upstream release');
    // alpha declares 2026-10-09T15:43:25Z; the cell shows the UTC day only and
    // carries the full canonical value in the time element's dateTime.
    expect(html).toContain('dateTime="2026-10-09T15:43:25Z">2026-10-09</time>');
    // beta and gamma declare nothing, so they show the em dash.
    expect(html.match(/>—<\/td>/g) ?? []).toHaveLength(2);
    expect(html).not.toContain('1970-01-01');
    expect(html).not.toContain('2026-02-31');
  });

  it('renders the same UTC date in both locales', async () => {
    const fixture = await createFixture();
    const en = await (await fixture.call(plain('/?lang=en'))).text();
    const zh = await (await fixture.call(plain('/?lang=zh'))).text();
    const cell = 'dateTime="2026-10-09T15:43:25Z">2026-10-09</time>';
    expect(en).toContain(cell);
    expect(zh).toContain(cell);
    expect(zh).toContain('上游发布');
  });

  it('offers one section navigation landmark with the three anchors', async () => {
    const fixture = await createFixture();
    for (const [path, label] of [
      ['/', 'aria-label="Sections"'],
      ['/?lang=zh', 'aria-label="页面导航"'],
    ] as const) {
      const html = await (await fixture.call(plain(path))).text();
      expect(html, path).toContain(label);
      // Exactly one navigation landmark: no duplicated desktop/mobile render.
      expect(html.match(/<nav /g) ?? [], path).toHaveLength(1);
      for (const href of ['href="#catalog"', 'href="#steps"', 'href="#usage"']) {
        expect(html, `${path} ${href}`).toContain(href);
      }
      // The anchors reach the real section identifiers and heading labels.
      for (const id of ['id="catalog"', 'id="steps"', 'id="usage"']) {
        expect(html, `${path} ${id}`).toContain(id);
      }
      expect(html, path).toContain('aria-labelledby="usage"');
    }
  });

  it('keeps the wide-screen layout hooks and the document order', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('<main class="page-content">');
    expect(html).toContain('class="catalog-section"');
    expect(html).toContain('class="steps-section mt-8"');
    expect(html).toContain('class="fair-use-section mt-8"');
    // The document order stays catalog, guide, usage: the wide grid places
    // the sections with CSS, never by reordering the markup.
    expect(html.indexOf('id="catalog"')).toBeLessThan(html.indexOf('id="steps"'));
    expect(html.indexOf('id="steps"')).toBeLessThan(html.indexOf('id="usage"'));
    // The page still carries exactly one navigation landmark.
    expect(html.match(/<nav /g) ?? []).toHaveLength(1);
  });

  it('states the fair-use budgets from the same constant in both locales', async () => {
    const fixture = await createFixture();
    const cases = [
      ['/', 'Fair use', 'Index and metadata requests', 'All package file requests',
        'One exact package file', 'requests/min',
        'If you are temporarily limited, wait about a minute and try again.'],
      ['/?lang=zh', '合理使用', '索引与元数据请求', '所有软件包文件请求',
        '同一个软件包文件', '次请求/分钟', '如果暂时受限，请等待约 1 分钟后重试。'],
    ] as const;

    for (const [path, heading, index, downloads, perFile, unit, retry] of cases) {
      const html = await (await fixture.call(plain(path))).text();
      expect(html, path).toContain(heading);
      expect(html, path).toContain(index);
      expect(html, path).toContain(downloads);
      expect(html, path).toContain(perFile);
      expect(html, path).toContain(retry);
      for (const limit of [120, 30, 6]) {
        expect(html, `${path} ${limit}`).toContain(`${limit} ${unit}`);
      }
      // No gateway internals leak into the page.
      for (const forbidden of ['namespace', 'per-colo', 'binding', 'CF-Connecting-IP']) {
        expect(html, `${path} ${forbidden}`).not.toContain(forbidden);
      }
    }
  });

  it('keeps the navigation and fair-use section when the catalog is unavailable', async () => {
    const fixture = await createFixture();
    fixture.bucket.entries.delete(`releases/${RELEASE_ID}/manifest.json`);
    const html = await (await fixture.call(plain('/'))).text();
    expect(html).toContain('aria-label="Sections"');
    expect(html).toContain('href="#catalog"');
    expect(html).toContain('href="#usage"');
    expect(html).toContain('Fair use');
    expect(html).toContain('120 requests/min');
  });
});

describe('command copy controls', () => {
  it('localizes the copy state labels for both locales', () => {
    expect(copyFor('en').commands).toEqual({
      copy: 'Copy',
      copied: 'Copied',
      failed: 'Copy failed',
    });
    expect(copyFor('zh').commands).toEqual({
      copy: '复制',
      copied: '已复制',
      failed: '复制失败',
    });
  });

  it('gives every command block its own localized copy button and live status', async () => {
    const fixture = await createFixture();
    for (const [path, label] of [
      ['/', 'Copy'],
      ['/?lang=zh', '复制'],
    ] as const) {
      const html = await (await fixture.call(plain(path))).text();
      const blocks = html.match(/<div class="command-block">[\s\S]*?<\/div><\/div>/g) ?? [];
      expect(blocks.length, path).toBeGreaterThan(0);
      for (const block of blocks) {
        expect(block, path).toContain('<pre class="code-block"><code>');
        expect(block, path).toMatch(/<button[^>]*type="button"/);
        expect(block, path).toContain('data-variant="outline"');
        expect(block, path).toContain(`<span>${label}</span>`);
        expect(block, path).toContain('role="status"');
        expect(block, path).toContain('aria-live="polite"');
      }
      // The collapsed fingerprint and device-credential commands carry the
      // same control as the always-visible steps.
      expect(html, path).toContain('gpg --show-keys /etc/apt/keyrings/apt-at-home.asc');
      expect(html, path).toContain('/etc/apt/auth.conf.d/apt-at-home.conf');
    }
  });

  it('keeps the canonical heredoc command text inside its copyable block', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();
    const blocks = html.match(/<div class="command-block">[\s\S]*?<\/div><\/div>/g) ?? [];
    const sources = blocks.find((block) => block.includes('sudo tee')) ?? '';
    expect(sources).toContain("sudo tee /etc/apt/sources.list.d/apt-at-home.sources");
    expect(sources).toContain('Types: deb');
    expect(sources).toContain('EOF');
    expect(sources).toMatch(/<button[^>]*type="button"/);
  });
});

describe('public browser assets', () => {
  it('serves the built assets without a credential', async () => {
    const fixture = await createFixture();
    fixture.assets.seed(CLIENT_SCRIPT, 'console.log("client")', 'text/javascript');
    fixture.assets.seed(CLIENT_STYLE, 'body{color:#000}', 'text/css');
    for (const [name, type] of [
      [CLIENT_SCRIPT, 'text/javascript'],
      [CLIENT_STYLE, 'text/css'],
      ['theme-init.js', 'text/javascript'],
    ] as const) {
      const response = await fixture.call(plain(`/assets/${name}`));
      expect(response.status, name).toBe(200);
      expect(response.headers.get('Content-Type'), name).toBe(type);
      expect(response.headers.get('X-Content-Type-Options'), name).toBe('nosniff');
    }
    expect(fixture.assets.requests).toEqual([CLIENT_SCRIPT, CLIENT_STYLE, 'theme-init.js']);
    expect(fixture.bucket.calls).toEqual([]);
  });

  it('answers HEAD for an asset without a body', async () => {
    const fixture = await createFixture();
    fixture.assets.seed(CLIENT_SCRIPT, 'console.log("client")', 'text/javascript');
    const response = await fixture.call(plain(`/assets/${CLIENT_SCRIPT}`, { method: 'HEAD' }));
    expect(response.status).toBe(200);
    expect(await response.text()).toBe('');
  });

  it('refuses every asset name that is not one flat script or stylesheet', async () => {
    const fixture = await createFixture();
    const refused = [
      '/assets/',
      '/assets/.vite/manifest.json',
      '/assets/main.js.map',
      '/assets/main.css.map',
      '/assets/manifest.json',
      '/assets/index.html',
      '/assets/.hidden.js',
      '/assets/sub/dir/main.js',
    ];
    for (const path of refused) {
      const response = await fixture.call(plain(path));
      expect(response.status, path).toBe(404);
    }
    expect(fixture.assets.requests).toEqual([]);
    expect(fixture.bucket.calls).toEqual([]);
  });

  it('ignores the query string of an asset request and never reflects it', async () => {
    const fixture = await createFixture();
    fixture.assets.seed(CLIENT_SCRIPT, 'console.log("client")', 'text/javascript');
    const response = await fixture.call(plain(`/assets/${CLIENT_SCRIPT}?token=SECRET`));
    expect(response.status).toBe(200);
    expect(await response.text()).not.toContain('SECRET');
    expect(fixture.assets.requests).toEqual([CLIENT_SCRIPT]);
  });

  it('never treats a path that leaves the asset prefix as an asset', async () => {
    const fixture = await createFixture();
    // The request line is normalised before the Worker sees it, so these
    // paths are ordinary repository paths and stay behind the credential gate.
    for (const path of [
      '/assets/theme-init.js/../../key.asc',
      '/assets/%2e%2e/key.asc',
    ]) {
      const response = await fixture.call(plain(path));
      expect(response.status, path).toBe(401);
    }
    expect(fixture.assets.requests).toEqual([]);
  });

  it('refuses an unknown asset with a 404', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(plain('/assets/missing.js'));
    expect(response.status).toBe(404);
    expect(fixture.assets.requests).toEqual(['missing.js']);
  });

  it('refuses any method but GET and HEAD on an asset', async () => {
    const fixture = await createFixture();
    const response = await fixture.call(
      plain(`/assets/${CLIENT_SCRIPT}`, { method: 'POST' }),
    );
    expect(response.status).toBe(405);
    expect(response.headers.get('Allow')).toBe('GET, HEAD');
    expect(fixture.assets.requests).toEqual([]);
  });
});

/** The class list of a document, reduced to what the theme code touches. */
class FakeClassList {
  private readonly names = new Set<string>();

  add(...names: string[]): void {
    for (const name of names) this.names.add(name);
  }

  remove(...names: string[]): void {
    for (const name of names) this.names.delete(name);
  }

  contains(name: string): boolean {
    return this.names.has(name);
  }

  get list(): string[] {
    return [...this.names].sort();
  }
}

describe('the theme bootstrap', () => {
  const source = readSource('../../web/public/theme-init.js');

  /** Runs the bootstrap against a document and a browser double. */
  function bootstrap(options: {
    stored?: string | null;
    storageDenied?: boolean;
    prefersDark?: boolean;
  }): FakeClassList {
    const classList = new FakeClassList();
    const storage = {
      getItem() {
        if (options.storageDenied === true) throw new Error('storage denied');
        return options.stored ?? null;
      },
    };
    const window = {
      localStorage: storage,
      matchMedia: () => ({ matches: options.prefersDark === true }),
    };
    const document = { documentElement: { classList } };
    new Function('window', 'document', source)(window, document);
    return classList;
  }

  it('marks the document with the saved preference', () => {
    expect(bootstrap({ stored: 'dark', prefersDark: false }).list).toEqual(['dark']);
    expect(bootstrap({ stored: 'light', prefersDark: true }).list).toEqual(['light']);
  });

  it('resolves the system preference when nothing is saved', () => {
    expect(bootstrap({ prefersDark: true }).list).toEqual(['dark']);
    expect(bootstrap({ prefersDark: false }).list).toEqual(['light']);
  });

  it('still resolves the system preference when storage is refused', () => {
    expect(bootstrap({ storageDenied: true, prefersDark: true }).list).toEqual(['dark']);
    expect(bootstrap({ storageDenied: true, prefersDark: false }).list).toEqual(['light']);
  });

  it('never leaves the document without a marker', () => {
    for (const options of [
      { stored: 'dark' },
      { stored: 'light' },
      { storageDenied: true },
      { prefersDark: true },
      { prefersDark: false },
    ]) {
      const classList = bootstrap(options);
      expect(classList.list.length, JSON.stringify(options)).toBe(1);
      expect(['light', 'dark']).toContain(classList.list[0]);
    }
  });
});

describe('the theme markers', () => {
  it('keeps the light and dark markers mutually exclusive', () => {
    const classList = new FakeClassList();
    markTheme({ classList }, 'dark');
    expect(classList.list).toEqual(['dark']);
    markTheme({ classList }, 'light');
    expect(classList.list).toEqual(['light']);
  });
});

describe('the wide-screen layout', () => {
  const styles = readSource('../../web/src/styles/app.css');

  it('bounds the page and puts the guide beside the catalog on a wide screen', () => {
    // The page grows with the window and stops at 1440 pixels.
    expect(styles).toMatch(/\.page \{[^}]*max-width: 90rem;/);
    // The shell gains its navigation column at 64rem, the content grid at 90rem.
    expect(styles).toMatch(/@media \(min-width: 64rem\)/);
    expect(styles).toMatch(/@media \(min-width: 90rem\)/);
    expect(styles).toMatch(/grid-template-columns: 10rem minmax\(0, 1fr\)/);
    expect(styles).toMatch(/grid-template-columns: minmax\(0, 1fr\) 25rem/);
    // The fair-use note spans both columns underneath them.
    expect(styles).toMatch(/\.fair-use-section \{[^}]*grid-column: 1 \/ -1;/);
  });

  it('carries the wide layout into the stylesheet the browser loads', () => {
    const built = readSource(`../dist/assets/${CLIENT_STYLE}`);
    // The minifier may write the range either way round.
    expect(built).toMatch(/@media \((?:min-width: ?|width>=)64rem\)/);
    expect(built).toMatch(/@media \((?:min-width: ?|width>=)90rem\)/);
  });
});

describe('command readability and navigation styling', () => {
  const styles = readSource('../../web/src/styles/app.css');
  const built = readSource(`../dist/assets/${CLIENT_STYLE}`);

  it('wraps command text instead of clipping it horizontally', () => {
    expect(styles).toMatch(/\.code-block \{[^}]*white-space: pre-wrap;/);
    expect(styles).toMatch(/\.code-block \{[^}]*overflow-wrap: anywhere;/);
    expect(styles).not.toMatch(/\.code-block \{[^}]*white-space: pre;/);
    expect(styles).toMatch(/\.command-block \{[^}]*min-width: 0;/);
    expect(built).toContain('white-space:pre-wrap');
    expect(built).toContain('overflow-wrap:anywhere');
  });

  it('scrolls smoothly only when motion is not reduced and keeps the anchor margin', () => {
    expect(styles).toMatch(
      /@media \(prefers-reduced-motion: no-preference\) \{[\s\S]*?scroll-behavior: smooth;/,
    );
    expect(styles.match(/scroll-behavior: smooth/g) ?? []).toHaveLength(1);
    expect(styles).toMatch(/main h2\[id\] \{[^}]*scroll-margin-top:/);
    expect(built).toContain('prefers-reduced-motion:no-preference');
    expect(built).toContain('scroll-behavior:smooth');
  });

  it('uses coordinated blue primary, accent and ring tokens in both palettes', () => {
    for (const token of [
      '--light-primary',
      '--light-accent',
      '--light-ring',
      '--dark-primary',
      '--dark-accent',
      '--dark-ring',
    ]) {
      const value = styles.match(new RegExp(`${token}: ([^;]+);`))?.[1] ?? '';
      expect(value, token).toMatch(/^oklch\(0\.\d+ 0\.\d+ 256\)$/);
    }
  });
});

describe('the stylesheet fallback', () => {
  const styles = readSource('../../web/src/styles/app.css');
  const built = readSource(`../dist/assets/${CLIENT_STYLE}`);

  it('applies the dark palette under the system preference only without a marker', () => {
    expect(styles).toMatch(/@media \(prefers-color-scheme: dark\)/);
    expect(styles).toMatch(/:root:not\(\.light\):not\(\.dark\)/);
    expect(styles).toMatch(/^\.dark \{/m);
    expect(styles).toMatch(/^\.light \{/m);
  });

  it('carries the system fallback into the stylesheet the browser loads', () => {
    expect(built).toContain('prefers-color-scheme:dark');
    expect(built).toContain(':root:not(.light):not(.dark)');
  });

  it('declares each palette value once and reuses it', () => {
    // The dark background value appears once as a palette entry; the two
    // blocks that use it only name the variable.
    const value = '--dark-background: oklch(0.145 0 0);';
    expect(styles.split(value).length - 1).toBe(1);
    expect(styles).toMatch(/--background: var\(--dark-background\);/);
    expect(styles).toMatch(/--background: var\(--light-background\);/);
  });
});
