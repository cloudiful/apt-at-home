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
  readonly publishedAtColumn: string
  readonly publishedAtUnknown: string
  readonly publishedToday: string
  readonly publishedYesterday: string
  readonly publishedDaysAgo: string
  readonly unavailable: string
}

/** One in-page section shortcut. */
export interface NavSectionCopy {
  readonly href: string
  readonly label: string
}

/** Prose for the section navigation. */
export interface NavCopy {
  readonly label: string
  readonly sections: readonly NavSectionCopy[]
}

/** Prose for the fair-use section; the numbers come from REQUEST_LIMITS. */
export interface FairUseCopy {
  readonly heading: string
  readonly summary: string
  readonly limits: {
    readonly index: string
    readonly downloads: string
    readonly perFile: string
  }
  readonly unit: string
  readonly retry: string
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

/** Labels for a command block's copy control. */
export interface CommandCopy {
  readonly copy: string
  readonly copied: string
  readonly failed: string
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

/** Prose for the anonymous public request counters. */
export interface PublicStatsCopy {
  readonly heading: string
  readonly visits: string
  readonly downloads: string
  readonly note: string
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
  readonly nav: NavCopy
  readonly catalog: CatalogCopy
  readonly stepsHeading: string
  readonly steps: InstallStepsCopy
  readonly commands: CommandCopy
  readonly fairUse: FairUseCopy
  readonly publicStats: PublicStatsCopy
}

/**
 * The request budgets the fair-use section states, in requests per minute.
 *
 * The numbers live in one place so the bilingual copy cannot drift from the
 * deployed limits; the deployment test binds them to the Worker's own
 * rate-limit bindings.
 */
export const REQUEST_LIMITS = {
  index: 120,
  downloads: 30,
  perFile: 6,
} as const

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
  nav: {
    label: 'Sections',
    sections: [
      { href: '#catalog', label: 'Packages' },
      { href: '#steps', label: 'Install' },
      { href: '#usage', label: 'Usage limits' },
    ],
  },
  catalog: {
    heading: 'Published packages',
    caption: 'Published packages',
    packageColumn: 'Package',
    versionColumn: 'Upstream version',
    architectureColumn: 'Architecture',
    publishedAtColumn: 'Upstream release',
    publishedAtUnknown: '—',
    publishedToday: 'Today',
    publishedYesterday: 'Yesterday',
    publishedDaysAgo: '{days} days ago',
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
  commands: {
    copy: 'Copy',
    copied: 'Copied',
    failed: 'Copy failed',
  },
  fairUse: {
    heading: 'Fair use',
    summary:
      'Requests are budgeted per shared public IP per minute, so a shared network, office, or proxy draws on the same budget.',
    limits: {
      index: 'Index and metadata requests',
      downloads: 'All package file requests',
      perFile: 'One exact package file',
    },
    unit: 'requests/min',
    retry: 'If you are temporarily limited, wait about a minute and try again.',
  },
  publicStats: {
    heading: 'Last 30 days',
    visits: 'Visits',
    downloads: 'Download requests',
    note: 'Approximate request counts, not unique visitors or completed installs.',
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
  nav: {
    label: '页面导航',
    sections: [
      { href: '#catalog', label: '软件包' },
      { href: '#steps', label: '安装' },
      { href: '#usage', label: '使用限制' },
    ],
  },
  catalog: {
    heading: '已发布的软件包',
    caption: '已发布的软件包',
    packageColumn: '软件包',
    versionColumn: '上游版本',
    architectureColumn: '架构',
    publishedAtColumn: '上游发布',
    publishedAtUnknown: '—',
    publishedToday: '今天',
    publishedYesterday: '昨天',
    publishedDaysAgo: '{days} 天前',
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
  commands: {
    copy: '复制',
    copied: '已复制',
    failed: '复制失败',
  },
  fairUse: {
    heading: '合理使用',
    summary:
      '请求按共享公网 IP 每分钟计量，共享网络、办公室或代理使用同一配额。',
    limits: {
      index: '索引与元数据请求',
      downloads: '所有软件包文件请求',
      perFile: '同一个软件包文件',
    },
    unit: '次请求/分钟',
    retry: '如果暂时受限，请等待约 1 分钟后重试。',
  },
  publicStats: {
    heading: '最近 30 天',
    visits: '访问次数',
    downloads: '下载请求数',
    note: '近似请求次数，不是独立访客数，也不是已完成的安装数。',
  },
}

const COPY: Record<Locale, HomepageCopy> = { en, zh }

/** The prose for one locale. */
export function copyFor(locale: Locale): HomepageCopy {
  return COPY[locale]
}
