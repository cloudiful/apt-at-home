import { AuthDetails, InstallSteps } from '@/components/install-steps'
import { Header } from '@/components/header'
import { PackageTable } from '@/components/package-table'
import { ThemeProvider } from '@/components/theme-provider'
import { copyFor } from '@/lib/locales'
import type { AppProps } from '@/lib/types'

/**
 * The public landing page.
 *
 * The same component renders the document on the server and hydrates it in the
 * browser, so the table and the commands always come from the published
 * catalog the server read. The page keeps its shape without JavaScript: the
 * markup is the document, and the client only takes over the controls.
 */
export function App({ locale, origin, packages }: AppProps) {
  const copy = copyFor(locale)

  return (
    <ThemeProvider>
      <div className="page">
        <Header locale={locale} copy={copy} />
        <main>
          <section aria-labelledby="catalog">
            <h2 id="catalog" className="mb-3 border-b pb-1.5 text-lg">
              {copy.catalog.heading}
            </h2>
            <PackageTable packages={packages} copy={copy} />
          </section>

          <section aria-labelledby="steps" className="mt-8">
            <h2 id="steps" className="mb-3 border-b pb-1.5 text-lg">
              {copy.stepsHeading}
            </h2>
            <InstallSteps origin={origin} packages={packages} copy={copy} />
            <AuthDetails origin={origin} copy={copy} />
          </section>
        </main>
      </div>
    </ThemeProvider>
  )
}
