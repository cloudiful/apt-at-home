/**
 * Relative upstream release age from fixed instants.
 *
 * The helper compares UTC calendar days, never local days or live clocks, so
 * the server markup and the hydrated page agree on one request-time reference.
 * Exact dates stay locale-independent `YYYY-MM-DD` slices of the canonical
 * instant.
 */
import type { CatalogCopy } from './locales'

/** Exact UTC calendar day of a canonical instant, without locale formatting. */
export function exactReleaseDate(publishedAt: string): string {
  return publishedAt.slice(0, 10)
}

/**
 * Relative label for one upstream instant against one request clock.
 *
 * Returns the localized age-first label, or `null` when no relative applies:
 * unknown, unparsable, or future UTC calendar days. Callers show the exact
 * date alone for a present-but-unusable value and the em dash for unknown,
 * mirroring the previous date-only behavior.
 */
export function relativeReleaseLabel(
  publishedAt: string | undefined,
  now: string,
  catalog: CatalogCopy,
): string | null {
  if (publishedAt === undefined) return null
  const publishedTime = Date.parse(publishedAt)
  const nowTime = Date.parse(now)
  if (!Number.isFinite(publishedTime) || !Number.isFinite(nowTime)) return null
  // Reject normalized rollovers such as February 31: the UTC day of the
  // parsed instant must match the claimed calendar day.
  const claimedDay = publishedAt.slice(0, 10)
  const parsedDay = new Date(publishedTime).toISOString().slice(0, 10)
  if (parsedDay !== claimedDay) return null
  const diffDays = utcDayIndex(nowTime) - utcDayIndex(publishedTime)
  if (diffDays < 0) return null
  if (diffDays === 0) return catalog.publishedToday
  if (diffDays === 1) return catalog.publishedYesterday
  return catalog.publishedDaysAgo.replace('{days}', String(diffDays))
}

/** Whole UTC days since the epoch for one validated finite instant. */
function utcDayIndex(time: number): number {
  return Math.floor(time / 86_400_000)
}
