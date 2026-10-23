import type { PublicStatsCopy } from '@/lib/locales'
import type { Locale, PublicStats } from '@/lib/types'

/**
 * The anonymous request counters.
 *
 * The footer states what the numbers are: requests the server answered, not
 * people and not installations. Nothing here identifies a visitor, and the
 * counts are approximate, so the copy says so in the same breath.
 */
export function PublicStats({
  stats,
  copy,
  locale,
}: {
  stats: PublicStats
  copy: PublicStatsCopy
  locale: Locale
}) {
  return (
    <footer className="public-stats">
      <h2 className="public-stats-heading">{copy.heading}</h2>
      <dl className="public-stats-list">
        <div className="public-stats-item">
          <dt>{copy.visits}</dt>
          <dd>{formatCount(locale, stats.visits)}</dd>
        </div>
        <div className="public-stats-item">
          <dt>{copy.downloads}</dt>
          <dd>{formatCount(locale, stats.downloads)}</dd>
        </div>
      </dl>
      <p className="public-stats-note">{copy.note}</p>
    </footer>
  )
}

/**
 * Formats a counter for the page locale.
 *
 * The same formatter runs on the server and in the browser, so the rendered
 * document and the hydrated page always carry the same digits.
 */
function formatCount(locale: Locale, value: number): string {
  return new Intl.NumberFormat(locale).format(value)
}
