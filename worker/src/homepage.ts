/**
 * Public landing page for the exact repository root.
 *
 * `/` is the one path served before the credential gate. It reads only the
 * published release metadata — the release pointer and the manifest that
 * release binds — and never the credential secret, so an operator whose
 * runtime configuration is broken can still read how to bootstrap a client and
 * what the repository currently publishes. The catalog is optional: when the
 * metadata cannot be read the page still renders, with a short status instead
 * of a package table, and never with a stale or invented catalog.
 *
 * The guide is served in English or Simplified Chinese, chosen from an
 * explicit `lang` override or the browser's `Accept-Language`, and every other
 * path keeps the fail-closed authentication and storage order in `index.ts`.
 */
import { FAVICON_DATA_URI, renderHomepageBody } from './homepage-content.js';
import { HOMEPAGE_STYLES } from './homepage-styles.js';
import { documentLanguage, resolveLocale } from './i18n.js';
import { loadCatalog } from './packages.js';
import { methodNotAllowed } from './responses.js';
import type { PackageSummary } from './packages.js';
import type { Locale } from './i18n.js';
import type { Env } from './types.js';

/** Public cache lifetime for the page, in seconds. */
const HOMEPAGE_CACHE_CONTROL = 'public, max-age=60';

const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "style-src 'unsafe-inline'",
  'img-src data:',
  "base-uri 'none'",
  "frame-ancestors 'none'",
].join('; ');

const encoder = new TextEncoder();

/**
 * Serves the public root landing page, or returns `null` for any other path so
 * the caller falls through to authenticated serving.
 *
 * Dispatch is on the normalised URL pathname, so the page is served only for
 * the exact `/`. The query string is read solely for a supported `lang`
 * override; nothing else from it reaches the page. Only the exact root and the
 * two allowed methods reach storage, so every other path stays untouched.
 */
export async function handleRoot(request: Request, env: Env): Promise<Response | null> {
  const url = new URL(request.url);
  if (url.pathname !== '/') return null;
  if (request.method !== 'GET' && request.method !== 'HEAD') return methodNotAllowed();
  const locale = resolveLocale(url.searchParams.get('lang'), request.headers.get('accept-language'));
  const catalog = await loadCatalog(env);
  const packages: readonly PackageSummary[] | undefined = catalog.ok ? catalog.packages : undefined;
  return homepageResponse(request.method, url.origin, locale, packages);
}

function homepageResponse(
  method: string,
  origin: string,
  locale: Locale,
  packages?: readonly PackageSummary[],
): Response {
  const document = renderHomepageDocument(origin, locale, packages);
  const length = encoder.encode(document).byteLength;
  const headers = {
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Length': String(length),
    'Content-Language': documentLanguage(locale),
    'Cache-Control': HOMEPAGE_CACHE_CONTROL,
    'Content-Security-Policy': CONTENT_SECURITY_POLICY,
    Vary: 'Accept-Language',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
  };
  // HEAD advertises the GET length but carries no payload.
  return new Response(method === 'HEAD' ? null : document, { status: 200, headers });
}

/** Renders the complete HTML document for one request origin and locale. */
export function renderHomepageDocument(
  origin: string,
  locale: Locale,
  packages?: readonly PackageSummary[],
): string {
  return `<!doctype html>
<html lang="${documentLanguage(locale)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>apt-at-home</title>
<link rel="icon" href="${FAVICON_DATA_URI}">
<style>${HOMEPAGE_STYLES}</style>
</head>
<body>
${renderHomepageBody(origin, locale, packages)}
</body>
</html>
`;
}
