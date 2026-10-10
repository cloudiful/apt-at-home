/**
 * User-facing content for the public root landing page.
 *
 * Every visible string lives here so the layout in `homepage.ts` stays a thin
 * assembler. The only value taken from the request is the origin, and it is
 * HTML-escaped before it is interpolated, so a crafted hostname can never inject
 * markup into the page.
 */

/** Public repository the guide links to; carries no account or bucket detail. */
export const REPOSITORY_URL = 'https://github.com/cloudiful/apt-at-home';

/** Inline favicon so the page needs no external asset or network fetch. */
export const FAVICON_DATA_URI =
  "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E%3Crect%20width='32'%20height='32'%20rx='7'%20fill='%232563eb'/%3E%3Ctext%20x='16'%20y='23'%20font-family='monospace'%20font-size='19'%20font-weight='700'%20fill='%23ffffff'%20text-anchor='middle'%3EA%3C/text%3E%3C/svg%3E";

/** Escapes a value for interpolation into element text or a quoted attribute. */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Renders the page body for one request origin.
 *
 * The origin is shown as the `apt` source host and the `auth.conf` machine
 * entry, and the guide states the trust and credential prerequisites the
 * administrator must satisfy out of band.
 */
export function renderHomepageBody(origin: string): string {
  const escapedOrigin = escapeHtml(origin);
  return `<main>
  <header>
    <h1>apt-at-home</h1>
    <p class="lede">面向 Debian 13 的私有、逐设备认证的 APT 软件源，提供 <strong>zellij</strong> 与 <strong>starship</strong> 的 amd64、arm64 软件包。</p>
  </header>

  <section aria-labelledby="prerequisites">
    <h2 id="prerequisites">开始之前</h2>
    <p>请向管理员获取本设备专属的<strong>用户名与令牌</strong>，以及<strong>完整的受信任主密钥指纹</strong>。软件源必须已完成首次发布，否则下面的命令没有可安装的内容。本页只说明如何配置，并不表示软件源已经就绪。</p>
  </section>

  <section aria-labelledby="steps">
    <h2 id="steps">安装步骤</h2>
    <ol class="steps">
      <li>
        <h3>准备凭据文件</h3>
        <p>创建密钥环与认证目录，用 <code>sudo touch</code> 建立文件后再 <code>sudo chmod 600</code>，然后用 <code>sudoedit</code> 写入。下面只是模板，<code>DEVICE</code> 与 <code>TOKEN</code> 是占位符：不要把真实令牌写进可执行脚本、命令行参数或 URL，服务器不提供任何凭据创建脚本。</p>
        <pre><code>sudo mkdir -p /etc/apt/keyrings /etc/apt/auth.conf.d
sudo touch /etc/apt/auth.conf.d/apt-at-home.conf
sudo chmod 600 /etc/apt/auth.conf.d/apt-at-home.conf
sudoedit /etc/apt/auth.conf.d/apt-at-home.conf</code></pre>
        <p>文件内容（<code>machine</code> 使用完整来源地址）：</p>
        <pre><code>machine ${escapedOrigin} login DEVICE password TOKEN</code></pre>
      </li>
      <li>
        <h3>获取并校验签名公钥</h3>
        <p>用 <code>curl</code> 取回公钥（由 curl 提示输入密码，不使用 <code>--location</code>）：</p>
        <pre><code>curl --fail --show-error --user DEVICE --output key.asc ${escapedOrigin}/key.asc
gpg --show-keys --with-fingerprint key.asc</code></pre>
        <p>在导入前，用单独渠道收到的<strong>完整主密钥指纹</strong>逐字比对，不要信任本页或网关给出的指纹。确认一致后再转换为二进制密钥环并安装：</p>
        <pre><code>gpg --dearmor --output apt-at-home.gpg key.asc
sudo install -m0644 apt-at-home.gpg /etc/apt/keyrings/apt-at-home.gpg</code></pre>
        <p>apt 使用的是二进制 <code>.gpg</code> 密钥环，而不是 armored 的 <code>.asc</code>；也不要把 apt 的 <code>auth.conf</code> 当作 curl 的 netrc 使用，两者的匹配语法不同。</p>
      </li>
      <li>
        <h3>添加软件源</h3>
        <p>用 <code>sudoedit /etc/apt/sources.list.d/apt-at-home.sources</code> 写入 Deb822 配置：</p>
        <pre><code>Types: deb
URIs: ${escapedOrigin}
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/apt-at-home.gpg</code></pre>
        <p>架构按本机 dpkg 自动选择，不需要强制指定外来架构。</p>
      </li>
      <li>
        <h3>更新并安装</h3>
        <pre><code>sudo apt update
sudo apt install zellij starship</code></pre>
      </li>
    </ol>
  </section>

  <footer>
    <p>更多说明见<a href="${REPOSITORY_URL}">公开仓库的 README 与 docs/setup.md</a>。软件包、索引与签名密钥仍保持私有，只有本页公开。</p>
  </footer>
</main>
`;
}
