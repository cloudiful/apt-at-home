import { GitHubLink } from '@/components/github-link'
import { LanguageMenu } from '@/components/language-menu'
import { ThemeToggle } from '@/components/theme-toggle'
import type { HomepageCopy } from '@/lib/locales'
import type { Locale } from '@/lib/types'

/**
 * The page header.
 *
 * The header carries the controls only: the language menu, the theme toggle
 * and the repository link, all aligned to the right. The page title stays for
 * assistive technology as a screen-reader-only heading, so the document still
 * has one first-level heading without showing the former title and subtitle.
 */
export function Header({ locale, copy }: { locale: Locale; copy: HomepageCopy }) {
  return (
    <header className="flex items-center justify-end gap-1 pb-4">
      <h1 className="sr-only">{copy.pageTitle}</h1>
      <LanguageMenu locale={locale} copy={copy} />
      <ThemeToggle copy={copy} />
      <GitHubLink copy={copy} />
    </header>
  )
}
