/**
 * Chinese prose for the public root landing page.
 *
 * Command and configuration blocks are not repeated here: they come from
 * `homepage-commands.ts` and are shared with every other locale. The package
 * roster is not written here either: it comes from the published catalog.
 */
import type { HomepageCopy } from './homepage-content.js';

export const homepageCopy: HomepageCopy = {
  languageLabel: '语言',
  catalog: {
    heading: '已发布的软件包',
    caption: '已发布的软件包',
    packageColumn: '软件包',
    versionColumn: '上游版本',
    architectureColumn: '架构',
    unavailable: '当前无法获取已发布的软件包列表。',
  },
  stepsHeading: '安装步骤',
  steps: {
    key: {
      title: '安装签名公钥',
      fingerprintSummary: '校验公钥指纹（可选）',
      fingerprintNote: '上面的下载会为获取公钥而信任本服务器。请用单独、独立可信渠道获得的完整主密钥指纹逐字比对，不要信任本页或网关给出的指纹。',
    },
    sources: {
      title: '添加软件源',
    },
    install: {
      title: '更新并安装',
      amd64Intro: '仅 amd64：',
      arm64Intro: '仅 arm64：',
    },
    auth: {
      summary: '设备凭据（仅在启用认证时）',
      intro: '当前软件源匿名提供下载。若管理员重新启用凭据校验，请写入凭据文件并使用设备凭据获取公钥；源条目本身不变。',
      fileLabel: '文件内容（<code>machine</code> 使用完整来源地址）：',
      curlNote: '启用校验后，获取公钥改用设备名并提示输入密码，而非上面的匿名命令：',
    },
  },
  footer: {
    linkText: '文档',
  },
};
