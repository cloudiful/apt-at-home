/**
 * Locale negotiation for the public root landing page.
 *
 * Only two locales are supported. The page is chosen from an explicit `lang`
 * query override first, then the request's `Accept-Language` header, and falls
 * back to English. Nothing here trusts the raw header or query as HTML: it only
 * maps either to one of the supported locale identifiers.
 */

/** Locales the landing page ships. English is the fallback. */
export const SUPPORTED_LOCALES = ['en', 'zh'] as const;

export type Locale = (typeof SUPPORTED_LOCALES)[number];

/** Locale served when the request expresses no supported preference. */
export const DEFAULT_LOCALE: Locale = 'en';

/** Cap on header characters parsed so a hostile header cannot force unbounded work. */
const MAX_ACCEPT_LANGUAGE_LENGTH = 4096;

export function isLocale(value: string): value is Locale {
  return (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

/**
 * Maps a `lang` query value to a supported locale, or `null` when it is absent
 * or unsupported. Matching is case-insensitive and only exact `en`/`zh` counts,
 * so an arbitrary query value is ignored rather than reflected.
 */
export function parseLocaleOverride(raw: string | null): Locale | null {
  if (raw === null) return null;
  const normalized = raw.trim().toLowerCase();
  return isLocale(normalized) ? normalized : null;
}

/**
 * Picks the best supported locale from an `Accept-Language` header.
 *
 * Entries are matched on their primary subtag, so `en-US` and `zh-Hant` count
 * as `en` and `zh`, then ordered by explicit `q` weight descending with the
 * first occurrence winning ties. Unsupported tags, `*`, and `q=0` entries are
 * skipped, and a malformed `q` parameter invalidates only its own entry. The
 * header is truncated to a bounded length before parsing, and a missing,
 * empty, or unsupported header falls back to English.
 */
export function negotiateLocale(acceptLanguage: string | null): Locale {
  if (acceptLanguage === null) return DEFAULT_LOCALE;
  const header = acceptLanguage.slice(0, MAX_ACCEPT_LANGUAGE_LENGTH);

  let best: Locale | null = null;
  let bestQuality = 0;

  for (const entry of header.split(',')) {
    const parsed = parseEntry(entry);
    if (parsed === null) continue;
    const locale = primaryLocale(parsed.tag);
    if (locale === null || parsed.quality <= 0) continue;
    if (best === null || parsed.quality > bestQuality) {
      best = locale;
      bestQuality = parsed.quality;
    }
  }

  return best ?? DEFAULT_LOCALE;
}

/** Resolves the page locale: an explicit supported override wins, else the header. */
export function resolveLocale(override: string | null, acceptLanguage: string | null): Locale {
  return parseLocaleOverride(override) ?? negotiateLocale(acceptLanguage);
}

/** Value for `html lang` and `Content-Language` for one locale. */
export function documentLanguage(locale: Locale): string {
  return locale === 'zh' ? 'zh-CN' : 'en';
}

interface ParsedEntry {
  readonly tag: string;
  readonly quality: number;
}

function parseEntry(entry: string): ParsedEntry | null {
  const [rawTag, ...params] = entry.split(';');
  const tag = (rawTag ?? '').trim();
  if (tag === '') return null;

  let quality = 1;
  for (const param of params) {
    const trimmed = param.trim();
    if (trimmed === '') continue;
    const separator = trimmed.indexOf('=');
    if (separator === -1) return null;
    const name = trimmed.slice(0, separator).trim().toLowerCase();
    if (name !== 'q') continue;
    const value = trimmed.slice(separator + 1).trim();
    if (value === '') return null;
    const qualityValue = Number(value);
    if (!Number.isFinite(qualityValue) || qualityValue < 0 || qualityValue > 1) return null;
    quality = qualityValue;
  }

  return { tag, quality };
}

function primaryLocale(tag: string): Locale | null {
  const primary = (tag.split('-')[0] ?? '').trim().toLowerCase();
  return isLocale(primary) ? primary : null;
}
