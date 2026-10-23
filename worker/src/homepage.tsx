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
 * The page is rendered by React on the server and hydrated in the browser, so
 * the same components produce the document and the interactive page. The
 * guide is served in English or Simplified Chinese, chosen from an explicit
 * `lang` override or the browser's `Accept-Language`, and every other path
 * keeps the fail-closed authentication and storage order in `index.ts`.
 */
import { renderToString } from 'react-dom/server.edge';
import { App } from '../../web/src/app';
import { CLIENT_SCRIPT_URL, CLIENT_STYLE_URLS, THEME_INIT_URL } from './assets.js';
import { documentLanguage, resolveLocale } from './i18n.js';
import { loadCatalog } from './packages.js';
import { methodNotAllowed } from './responses.js';
import type { AppProps, PackageSummary } from '../../web/src/lib/types';
import type { Locale } from './i18n.js';
import type { Env } from './types.js';

/** Public cache lifetime for the page, in seconds. */
const HOMEPAGE_CACHE_CONTROL = 'public, max-age=60';

/** Prefix every generated identifier carries, on the server and in the browser. */
const IDENTIFIER_PREFIX = 'apt';

/** Inline favicon so the page needs no external asset or network fetch. */
const FAVICON_DATA_URI =
  "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E%3Crect%20width='32'%20height='32'%20rx='7'%20fill='%232563eb'/%3E%3Ctext%20x='16'%20y='23'%20font-family='monospace'%20font-size='19'%20font-weight='700'%20fill='%23ffffff'%20text-anchor='middle'%3EA%3C/text%3E%3C/svg%3E";

const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'self'",
  // The dropdown positions its content with inline styles, so the style
  // source needs the inline allowance next to the stylesheet this origin
  // serves.
  "style-src 'self' 'unsafe-inline'",
  'img-src data: https://img.shields.io',
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
  const now = new Date().toISOString();
  const catalog = await loadCatalog(env);
  const packages: readonly PackageSummary[] | null = catalog.ok ? catalog.packages : null;
  return homepageResponse(request.method, url.origin, locale, packages, now);
}

function homepageResponse(
  method: string,
  origin: string,
  locale: Locale,
  packages: readonly PackageSummary[] | null,
  now: string,
): Response {
  const document = renderHomepageDocument({ locale, origin, packages, now });
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
export function renderHomepageDocument(props: AppProps): string {
  const body = renderToString(<App {...props} />, { identifierPrefix: IDENTIFIER_PREFIX });
  const styles = CLIENT_STYLE_URLS.map(
    (href) => `<link rel="stylesheet" href="${href}">`,
  ).join('\n');
  return `<!doctype html>
<html lang="${documentLanguage(props.locale)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>apt-at-home</title>
<link rel="icon" href="${FAVICON_DATA_URI}">
<script src="${THEME_INIT_URL}"></script>
${styles}
</head>
<body>
<div id="app">${body}</div>
<script id="app-props" type="application/json">${serializeProps(props)}</script>
<script src="${CLIENT_SCRIPT_URL}" type="module"></script>
</body>
</html>
`;
}

/**
 * Serializes the props the browser hydrates with.
 *
 * The payload is data, not markup, and every `<` is escaped, so a value that
 * ends the script element early can never leave the data block.
 */
function serializeProps(props: AppProps): string {
  return JSON.stringify(props).replace(/</g, '\\u003c');
}
