import { afterEach, describe, expect, it, vi } from 'vitest';

import gateway from '../src/index.js';
import { renderHomepageDocument } from '../src/homepage.js';
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
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('%3Cscript');
  });

  it('marks the active locale link and normalizes the switch hrefs', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/?lang=zh'))).text();
    expect(html).toContain('href="?lang=en"');
    expect(html).toContain('href="?lang=zh"');
    expect(html).toMatch(/<a href="\?lang=zh"[^>]*aria-current="true"/);
    expect(html).not.toMatch(/<a href="\?lang=en"[^>]*aria-current/);
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
      return html.match(/<pre><code>[\s\S]*?<\/code><\/pre>/g) ?? [];
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

    expect(html).toContain('<pre><code>sudo apt update</code></pre>');
    expect(html).not.toContain('Update the package index');
    expect(html).toContain('sudo install -d -m0755 /etc/apt/keyrings');
  });

  it('lists the published packages with their versions and architectures', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('<caption class="sr-only">Published packages</caption>');
    expect(html).toContain('<th scope="col">Package</th>');
    expect(html).toContain('<th scope="col">Upstream version</th>');
    expect(html).toContain('<th scope="col">Architecture</th>');
    expect(html).toContain('<th scope="row">alpha</th>');
    expect(html).toContain('<th scope="row">beta</th>');
    expect(html).toContain('<th scope="row">gamma</th>');
    expect(html).toContain('<td>1.2.3</td>');
    expect(html).toContain('<td>0.4.0</td>');
    expect(html).toContain('<td>2.0.0</td>');
    expect(html).toContain('<td>amd64, arm64</td>');
    expect(html).toContain('<td>amd64</td>');
    expect(html).toContain('<td>arm64</td>');
  });

  it('labels the table for assistive technology without repeating it visibly', async () => {
    const fixture = await createFixture();
    for (const [path, caption] of [
      ['/', 'Published packages'],
      ['/?lang=zh', '已发布的软件包'],
    ] as const) {
      const html = await (await fixture.call(plain(path))).text();
      expect(html, path).toContain(`<caption class="sr-only">${caption}</caption>`);
      expect(html, path).not.toContain('<caption>');
      expect(html, path).not.toContain('display: none');
    }
  });

  it('ends with one documentation link and no policy prose', async () => {
    const fixture = await createFixture();
    for (const [path, label] of [
      ['/', 'Documentation'],
      ['/?lang=zh', '文档'],
    ] as const) {
      const html = await (await fixture.call(plain(path))).text();
      const footer = html.slice(html.indexOf('<footer>'), html.indexOf('</footer>'));
      expect(footer, path).toContain(
        `<a href="https://github.com/cloudiful/apt-at-home">${label}</a>`,
      );
      expect(footer.match(/<a /g) ?? [], path).toHaveLength(1);
      expect(html, path).not.toContain('public test mode');
      expect(html, path).not.toContain('公开测试模式');
      expect(html, path).not.toContain('docs/setup.md');
    }
  });

  it('keeps the platform label and drops the lede and the prerequisites block', async () => {
    const fixture = await createFixture();
    for (const path of ['/', '/?lang=zh']) {
      const html = await (await fixture.call(plain(path))).text();
      expect(html, path).toContain('Debian 13');
      expect(html, path).not.toContain('class="lede"');
      expect(html, path).not.toContain('Before you start');
      expect(html, path).not.toContain('开始之前');
      expect(html, path).not.toContain('prerequisites');
    }
  });

  it('builds the install commands from the same published catalog', async () => {
    const fixture = await createFixture();
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('sudo apt update &amp;&amp; sudo apt install alpha');
    expect(html).toContain('Only on amd64:');
    expect(html).toContain('<pre><code>sudo apt install beta</code></pre>');
    expect(html).toContain('Only on arm64:');
    expect(html).toContain('<pre><code>sudo apt install gamma</code></pre>');
    expect(html).not.toContain('sudo apt install alpha beta');
  });

  it('shows the short status and no table when the catalog cannot be read', async () => {
    const fixture = await createFixture();
    fixture.bucket.entries.delete(`releases/${RELEASE_ID}/manifest.json`);
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('role="status"');
    expect(html).toContain('The published package list is unavailable right now.');
    expect(html).not.toContain('<table>');
    expect(html).not.toContain('sudo apt install');
    expect(html).toContain('<pre><code>sudo apt update</code></pre>');
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

    expect(html).not.toContain('<script>');
    expect(html).not.toContain('alert(1)');
    expect(html).toContain('The published package list is unavailable right now.');
    expect(html).not.toContain('<table>');
  });

  it('picks up a newly published package without a template change', async () => {
    const fixture = await createFixture();
    seedManifest(fixture.bucket, RELEASE_ID, [
      ...SYNTHETIC_PACKAGES,
      { name: 'delta', version: '3.1.4', architectures: ['amd64', 'arm64'] },
    ]);
    const html = await (await fixture.call(plain('/'))).text();

    expect(html).toContain('<th scope="row">delta</th>');
    expect(html).toContain('<td>3.1.4</td>');
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
      expect(html, path).not.toContain('<script');
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
      const html = renderHomepageDocument(
        'https://evil"</code><script>alert(1)</script>',
        locale,
      );
      expect(html, locale).not.toContain('<script>');
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
});
