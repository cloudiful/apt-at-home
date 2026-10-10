/**
 * Public landing page for the exact repository root.
 *
 * `/` is the one path served before the credential gate: the gateway answers
 * with a static installation guide and never reads the credential secret, the
 * bucket, the cache or any environment value, so an operator whose runtime
 * configuration is broken can still read how to bootstrap a client. Every other
 * path keeps the fail-closed authentication and storage order in `index.ts`.
 */
import { FAVICON_DATA_URI, renderHomepageBody } from './homepage-content.js';
import { HOMEPAGE_STYLES } from './homepage-styles.js';
import { methodNotAllowed } from './responses.js';

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
 * Dispatch is on the normalised URL pathname, so a query string is ignored and
 * can never reach the page; only the exact `/` is public.
 */
export function handleRoot(request: Request): Response | null {
  const url = new URL(request.url);
  if (url.pathname !== '/') return null;
  if (request.method !== 'GET' && request.method !== 'HEAD') return methodNotAllowed();
  return homepageResponse(request.method, url.origin);
}

function homepageResponse(method: string, origin: string): Response {
  const document = renderHomepageDocument(origin);
  const length = encoder.encode(document).byteLength;
  const headers = {
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Length': String(length),
    'Cache-Control': HOMEPAGE_CACHE_CONTROL,
    'Content-Security-Policy': CONTENT_SECURITY_POLICY,
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
  };
  // HEAD advertises the GET length but carries no payload.
  return new Response(method === 'HEAD' ? null : document, { status: 200, headers });
}

/** Renders the complete HTML document for one request origin. */
export function renderHomepageDocument(origin: string): string {
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>apt-at-home</title>
<link rel="icon" href="${FAVICON_DATA_URI}">
<style>${HOMEPAGE_STYLES}</style>
</head>
<body>
${renderHomepageBody(origin)}
</body>
</html>
`;
}
