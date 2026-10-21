/**
 * The bilingual prose the page renders.
 *
 * The command blocks are not repeated here: they are derived from the
 * published catalog and the request origin, so the two locales show exactly
 * the same commands. The package roster is not written here either: it comes
 * from the published catalog.
 */
import type { Locale } from './types'

/** Prose for the published-package section. */
export interface CatalogCopy {
  readonly heading: string
  readonly caption: string
  readonly packageColumn: string
  readonly versionColumn: string
  readonly architectureColumn: string
  readonly unavailable: string
}

/** Prose for the signing-key step; the command block is shared. */
export interface KeyStepCopy {
  readonly title: string
  readonly fingerprintSummary: string
  readonly fingerprintNote: string
}

/** Prose for the sources step. */
export interface SourcesStepCopy {
  readonly title: string
}

/** Prose for the update-and-install step. */
export interface InstallStepCopy {
  readonly title: string
  readonly amd64Intro: string
  readonly arm64Intro: string
}

/** Prose for the collapsed device-credential details. */
export interface AuthDetailsCopy {
  readonly summary: string
  readonly intro: string
  readonly fileLabel: string
  readonly curlNote: string
}

export interface InstallStepsCopy {
  readonly key: KeyStepCopy
  readonly sources: SourcesStepCopy
  readonly install: InstallStepCopy
  readonly auth: AuthDetailsCopy
}

/** The prose contract every locale satisfies. */
export interface HomepageCopy {
  readonly documentTitle: string
  readonly pageTitle: string
  readonly languageLabel: string
  readonly languageEnglish: string
  readonly languageChinese: string
  readonly themeLabel: string
  readonly themeToLight: string
  readonly themeToDark: string
  readonly githubLabel: string
  readonly githubStarsAlt: string
  readonly catalog: CatalogCopy
  readonly stepsHeading: string
  readonly steps: InstallStepsCopy
}

const en: HomepageCopy = {
  documentTitle: 'apt-at-home',
  pageTitle: 'APT repository',
  languageLabel: 'Language',
  languageEnglish: 'English',
  languageChinese: '中文',
  themeLabel: 'Toggle theme',
  themeToLight: 'Switch to the light theme',
  themeToDark: 'Switch to the dark theme',
  githubLabel: 'apt-at-home on GitHub',
  githubStarsAlt: 'GitHub stars',
  catalog: {
    heading: 'Published packages',
    caption: 'Published packages',
    packageColumn: 'Package',
    versionColumn: 'Upstream version',
    architectureColumn: 'Architecture',
    unavailable: 'The published package list is unavailable right now.',
  },
  stepsHeading: 'Installation steps',
  steps: {
    key: {
      title: 'Install the signing key',
      fingerprintSummary: 'Verify the key fingerprint (optional)',
      fingerprintNote:
        'The download above trusts this server for the key. Compare the full primary fingerprint with a value you received through a separate, independently trusted channel; never trust a fingerprint printed by this page or the gateway.',
    },
    sources: {
      title: 'Add the repository',
    },
    install: {
      title: 'Update and install',
      amd64Intro: 'Only on amd64:',
      arm64Intro: 'Only on arm64:',
    },
    auth: {
      summary: 'Device credentials (only when authentication is enabled)',
      intro:
        'This repository currently serves downloads anonymously. If the administrator re-enables the credential gate, write a credential file and fetch the key with your device credentials instead; the source entry does not change.',
      fileLabel: 'File contents ({machine} uses the full source address):',
      curlNote:
        'With the gate enabled, the key fetch uses your device name and prompts for the password instead of the anonymous command above:',
    },
  },
}

const zh: HomepageCopy = {
  documentTitle: 'apt-at-home',
  pageTitle: 'APT 软件源',
  languageLabel: '语言',
  languageEnglish: 'English',
  languageChinese: '中文',
  themeLabel: '切换主题',
  themeToLight: '切换到浅色主题',
  themeToDark: '切换到深色主题',
  githubLabel: 'apt-at-home 的 GitHub 仓库',
  githubStarsAlt: 'GitHub 星标数',
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
      fingerprintNote:
        '上面的下载会为获取公钥而信任本服务器。请用单独、独立可信渠道获得的完整主密钥指纹逐字比对，不要信任本页或网关给出的指纹。',
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
      intro:
        '当前软件源匿名提供下载。若管理员重新启用凭据校验，请写入凭据文件并使用设备凭据获取公钥；源条目本身不变。',
      fileLabel: '文件内容（{machine} 使用完整来源地址）：',
      curlNote:
        '启用校验后，获取公钥改用设备名并提示输入密码，而非上面的匿名命令：',
    },
  },
}

const COPY: Record<Locale, HomepageCopy> = { en, zh }

/** The prose for one locale. */
export function copyFor(locale: Locale): HomepageCopy {
  return COPY[locale]
}
