/**
 * Public landing page for the exact repository root.
 *
 * `/` is the one path served before the credential gate: the gateway answers
 * with a static installation guide and never reads the credential secret, the
 * bucket, the cache or any environment value, so an operator whose runtime
 * configuration is broken can still read how to bootstrap a client. The guide
 * is served in English or Simplified Chinese, chosen from an explicit `lang`
 * override or the browser's `Accept-Language`, and every other path keeps the
 * fail-closed authentication and storage order in `index.ts`.
 */
import { FAVICON_DATA_URI, renderHomepageBody } from './homepage-content.js';
import { HOMEPAGE_STYLES } from './homepage-styles.js';
import { documentLanguage, resolveLocale } from './i18n.js';
import { methodNotAllowed } from './responses.js';
import type { Locale } from './i18n.js';

/** Public cache lifetime for the static page, in seconds. */
const HOMEPAGE_CACHE_CONTROL = 'public, max-age=3600';

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
 * override; nothing else from it reaches the page.
 */
export function handleRoot(request: Request): Response | null {
  const url = new URL(request.url);
  if (url.pathname !== '/') return null;
  if (request.method !== 'GET' && request.method !== 'HEAD') return methodNotAllowed();
  const locale = resolveLocale(url.searchParams.get('lang'), request.headers.get('accept-language'));
  return homepageResponse(request.method, url.origin, locale);
}

function homepageResponse(method: string, origin: string, locale: Locale): Response {
  const document = renderHomepageDocument(origin, locale);
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
export function renderHomepageDocument(origin: string, locale: Locale): string {
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
${renderHomepageBody(origin, locale)}
</body>
</html>
`;
}
