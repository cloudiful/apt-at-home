/**
 * Chinese prose for the public root landing page.
 *
 * Command and configuration blocks are not repeated here: they come from
 * `homepage-commands.ts` and are shared with every other locale.
 */
import type { HomepageCopy } from './homepage-content.js';

export const homepageCopy: HomepageCopy = {
  lede: '面向 Debian 13 的私有、逐设备认证的 APT 软件源，提供 <strong>zellij</strong> 与 <strong>starship</strong> 的 amd64、arm64 软件包。',
  languageLabel: '语言',
  prerequisites: {
    heading: '开始之前',
    body: '请向管理员获取本设备专属的<strong>用户名与令牌</strong>，以及<strong>完整的受信任主密钥指纹</strong>。软件源必须已完成首次发布，否则下面的命令没有可安装的内容。本页只说明如何配置，并不表示软件源已经就绪。',
  },
  stepsHeading: '安装步骤',
  steps: {
    prepare: {
      title: '准备凭据文件',
      intro: '创建密钥环与认证目录，用 <code>sudo touch</code> 建立文件后再 <code>sudo chmod 600</code>，然后用 <code>sudoedit</code> 写入。下面只是模板，<code>DEVICE</code> 与 <code>TOKEN</code> 是占位符：不要把真实令牌写进可执行脚本、命令行参数或 URL，服务器不提供任何凭据创建脚本。',
      entryLabel: '文件内容（<code>machine</code> 使用完整来源地址）：',
    },
    key: {
      title: '获取并校验签名公钥',
      fetchIntro: '用 <code>curl</code> 取回公钥（由 curl 提示输入密码，不使用 <code>--location</code>）：',
      verifyIntro: '在导入前，用单独渠道收到的<strong>完整主密钥指纹</strong>逐字比对，不要信任本页或网关给出的指纹。确认一致后再转换为二进制密钥环并安装：',
      installIntro: 'apt 使用的是二进制 <code>.gpg</code> 密钥环，而不是 armored 的 <code>.asc</code>；也不要把 apt 的 <code>auth.conf</code> 当作 curl 的 netrc 使用，两者的匹配语法不同。',
    },
    sources: {
      title: '添加软件源',
      intro: '用 <code>sudoedit /etc/apt/sources.list.d/apt-at-home.sources</code> 写入 Deb822 配置：',
      architectureNote: '架构按本机 dpkg 自动选择，不需要强制指定外来架构。',
    },
    install: {
      title: '更新并安装',
    },
  },
  footer: {
    before: '更多说明见',
    linkText: '公开仓库的 README 与 docs/setup.md',
    after: '。软件包、索引与签名密钥仍保持私有，只有本页公开。',
  },
};
