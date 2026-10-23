/**
 * The contract the server and the browser share.
 *
 * The server renders the page from these props and hands the very same object
 * to the browser, so the two trees match exactly. Everything here is plain
 * data: the props are serialized into the document and read back by the client
 * entry, so a prop that is not JSON would break the page.
 */

/** Locales the page ships. English is the fallback. */
export const SUPPORTED_LOCALES = ['en', 'zh'] as const

export type Locale = (typeof SUPPORTED_LOCALES)[number]

/** One published package, reduced to the fields the page shows. */
export interface PackageSummary {
  readonly name: string
  readonly version: string
  readonly architectures: readonly string[]
  /** Canonical UTC publication instant of the exact upstream release, if known. */
  readonly upstreamPublishedAt?: string
}

/**
 * The anonymous request counters the public page shows.
 *
 * The numbers count requests, never people: there is no visitor identity, no
 * cookie and no address in them. A visit is one served homepage request and a
 * download is one served package request, so both are approximate by nature.
 */
export interface PublicStats {
  /** Length of the rolling window the counters cover. */
  readonly days: 30
  /** Homepage requests that answered with the page. */
  readonly visits: number
  /** Package file requests that answered with the file. */
  readonly downloads: number
}

/** Everything the page renders from. */
export interface AppProps {
  /** Locale the page is served in. */
  readonly locale: Locale
  /** Origin the install commands are built from. */
  readonly origin: string
  /** Published catalog, or null when it could not be read. */
  readonly packages: readonly PackageSummary[] | null
  /** Request-time UTC clock reference, serialized for deterministic SSR/hydration. */
  readonly now: string
  /**
   * Anonymous request counters, or absent when they could not be read.
   *
   * The page omits the counters entirely in that case: an absent counter and a
   * counter of zero are different facts and must not look alike.
   */
  readonly stats?: PublicStats
}
