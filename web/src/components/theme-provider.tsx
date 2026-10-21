import * as React from 'react'

/**
 * The theme the page is rendered in.
 *
 * The server and the first browser render agree on "unknown", so the tree
 * never differs between the two. The saved preference or the system
 * preference is read after mount, and the inline bootstrap script has already
 * applied the same choice to the document before the first paint.
 */
export type Theme = 'light' | 'dark'

/** Key the saved preference is stored under. */
export const THEME_STORAGE_KEY = 'apt-theme'

/** The marker the document carries for the theme in effect. */
export type ThemeMarker = 'light' | 'dark'

interface ThemeContextValue {
  /** The effective theme, or null while it is still unknown. */
  readonly theme: Theme | null
  readonly toggle: () => void
}

const ThemeContext = React.createContext<ThemeContextValue>({
  theme: null,
  toggle: () => undefined,
})

/** The current theme and the toggle that switches it. */
export function useTheme(): ThemeContextValue {
  return React.useContext(ThemeContext)
}

/**
 * Marks the document with the theme in effect.
 *
 * The two markers are mutually exclusive: the document carries exactly one of
 * them, so the stylesheet never has to guess and the system preference can
 * never override an explicit choice.
 */
export function markTheme(
  root: { classList: { remove: (...names: string[]) => void; add: (...names: string[]) => void } },
  theme: Theme,
): void {
  root.classList.remove('light', 'dark')
  root.classList.add(theme)
}

/**
 * Provides the theme to the page.
 *
 * The preference is read once, after mount: the server cannot know it and the
 * browser must not read it during the first render, or the two trees would
 * differ. The document is marked with the theme, and the choice survives a
 * reload because it is stored.
 */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = React.useState<Theme | null>(null)

  React.useEffect(() => {
    const stored = readStoredTheme()
    const effective = stored ?? systemTheme()
    markTheme(document.documentElement, effective)
    setTheme(effective)
  }, [])

  const toggle = React.useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark'
      markTheme(document.documentElement, next)
      storeTheme(next)
      return next
    })
  }, [])

  const value = React.useMemo(() => ({ theme, toggle }), [theme, toggle])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

/** The saved preference, or null when there is none or storage is refused. */
function readStoredTheme(): Theme | null {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return stored === 'light' || stored === 'dark' ? stored : null
  } catch {
    return null
  }
}

/** The preference the operating system expresses. */
function systemTheme(): Theme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** Stores the preference, and keeps working when storage is refused. */
function storeTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // A browser that refuses storage still shows the chosen theme.
  }
}
