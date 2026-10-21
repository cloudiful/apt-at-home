import { Moon, Sun } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { useTheme } from '@/components/theme-provider'
import type { HomepageCopy } from '@/lib/locales'

/**
 * The theme toggle.
 *
 * The button is rendered the same way on the server and in the first browser
 * render: the effective theme is only known after mount, so the label names
 * the action and the pressed state appears with the theme it describes.
 */
export function ThemeToggle({ copy }: { copy: HomepageCopy }) {
  const { theme, toggle } = useTheme()
  const dark = theme === 'dark'
  const label =
    theme === null ? copy.themeLabel : dark ? copy.themeToLight : copy.themeToDark

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      onClick={toggle}
      aria-label={label}
      title={label}
      aria-pressed={theme === null ? undefined : dark}
    >
      {dark ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
    </Button>
  )
}
