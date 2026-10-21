import { Languages } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { HomepageCopy } from '@/lib/locales'
import type { Locale } from '@/lib/types'

/**
 * The language menu.
 *
 * Both entries are links to the same page with the locale the server
 * negotiates, so choosing one is a normal navigation and the server stays the
 * single source of the language. The current locale is marked in the menu.
 */
export function LanguageMenu({ locale, copy }: { locale: Locale; copy: HomepageCopy }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={copy.languageLabel}
          title={copy.languageLabel}
        >
          <Languages aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem asChild>
          <a
            href="?lang=en"
            hrefLang="en"
            lang="en"
            aria-current={locale === 'en' ? 'true' : undefined}
          >
            {copy.languageEnglish}
          </a>
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <a
            href="?lang=zh"
            hrefLang="zh-CN"
            lang="zh-CN"
            aria-current={locale === 'zh' ? 'true' : undefined}
          >
            {copy.languageChinese}
          </a>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
