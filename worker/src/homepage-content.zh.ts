/**
 * Chinese prose for the public root landing page.
 *
 * Command and configuration blocks are not repeated here: they come from
 * `homepage-commands.ts` and are shared with every other locale.
 */
import type { HomepageCopy } from './homepage-content.js';

export const homepageCopy: HomepageCopy = {
  lede: '面向 Debian 13 的公开测试 APT 软件源，提供 <strong>bottom</strong>、<strong>bun</strong>、<strong>git-delta</strong>、<strong>kandev</strong>、<strong>neovim</strong>、<strong>rustfs-cli</strong>、<strong>sccache</strong>、<strong>starship</strong>、<strong>uv</strong>、<strong>zellij</strong> 与 <strong>zoxide</strong> 的 amd64、arm64 软件包，另有仅 amd64 的 <strong>cargo-tauri</strong>。',
  languageLabel: '语言',
  prerequisites: {
    heading: '开始之前',
    body: '本指南需要已安装 <code>curl</code> 的 Debian 13，只说明如何配置客户端。软件源必须已完成首次发布，否则没有可安装的内容。当前下载不需要凭据，因此该主机名等同于公开资源。',
  },
  stepsHeading: '安装步骤',
  steps: {
    key: {
      title: '安装签名公钥',
      intro: '将 armored 公钥下载到 apt 信任的密钥环：',
      fingerprintSummary: '校验公钥指纹（可选）',
      fingerprintNote: '上面的下载会为获取公钥而信任本服务器。请用单独、独立可信渠道获得的完整主密钥指纹逐字比对，不要信任本页或网关给出的指纹。',
    },
    sources: {
      title: '添加软件源',
      intro: '写入 Deb822 源条目，使用上面安装的公钥签名：',
      architectureNote: '架构按本机 dpkg 自动选择，不需要强制指定外来架构。',
    },
    install: {
      title: '更新并安装',
      intro: '安装已发布的软件包（<code>bottom</code> 提供 <code>btm</code>，<code>git-delta</code> 提供 <code>delta</code>，<code>rustfs-cli</code> 提供 <code>rc</code>，<code>uv</code> 提供 <code>uv</code> 和 <code>uvx</code>，<code>cargo-tauri</code>（仅 amd64）提供 <code>cargo-tauri</code> 与 <code>cargo tauri</code>，需要 <code>cargo</code>）。<code>kandev</code> 安装标准服务器套件；运行 <code>kandev</code> 才会启动服务器。zoxide 的 Bash 集成是可选的；如需使用，可自行将 <code>eval "$(zoxide init bash)"</code> 添加到 shell 配置中，安装不会修改该配置。',
      amd64Intro: '仅 amd64，还可安装 Tauri CLI：',
    },
    auth: {
      summary: '设备凭据（仅在启用认证时）',
      intro: '当前软件源匿名提供下载。若管理员重新启用凭据校验，请写入凭据文件并使用设备凭据获取公钥；源条目本身不变。',
      fileLabel: '文件内容（<code>machine</code> 使用完整来源地址）：',
      curlNote: '启用校验后，获取公钥改用设备名并提示输入密码，而非上面的匿名命令：',
    },
  },
  footer: {
    before: '更多说明见',
    linkText: '公开仓库的 README 与 docs/setup.md',
    after: '。仓库处于公开测试模式时，软件包、索引与签名密钥无需凭据即可下载。',
  },
};
