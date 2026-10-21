/**
 * Response builders shared by the gateway.
 *
 * Every response is `private` or `no-store` so neither the Cloudflare edge nor
 * an intermediary can retain authenticated repository content, and no builder
 * ever echoes a credential back to the client.
 */

const IMMUTABLE_CACHE_CONTROL = 'private, max-age=31536000, immutable';
const REVALIDATED_CACHE_CONTROL = 'private, no-cache';
const NO_STORE = 'no-store';
/** Advisory delay for a limited client; not the counter's exact reset. */
const RETRY_AFTER_SECONDS = '60';
const TOO_MANY_REQUESTS_BODY = 'Too Many Requests\n';

const GENERIC_BODY = 'error\n';
const GENERIC_CONTENT_TYPE = 'text/plain; charset=utf-8';

/** A byte range selected for a partial response. */
export interface ByteRange {
  readonly offset: number;
  readonly length: number;
}

function errorResponse(status: number, headers: Record<string, string>): Response {
  return new Response(GENERIC_BODY, {
    status,
    headers: { ...headers, 'Content-Type': GENERIC_CONTENT_TYPE, 'Cache-Control': NO_STORE },
  });
}

/** The requested object, or the path, is not servable. */
export function notFound(): Response {
  return errorResponse(404, {});
}

/** Authentication required or refused. Carries the challenge apt needs to retry. */
export function unauthorized(challenge: Record<string, string>): Response {
  return errorResponse(401, { ...challenge, 'Cache-Control': NO_STORE });
}

/**
 * The gateway cannot serve anything: the credential secret is unusable, or the
 * release pointer exists but does not select a complete release.
 */
export function serviceUnavailable(): Response {
  return errorResponse(503, {});
}

/** Only GET and HEAD are served, and only to an authenticated client. */
export function methodNotAllowed(): Response {
  return errorResponse(405, { Allow: 'GET, HEAD' });
}

/**
 * The artifact request budget is exhausted. The client is told to wait, but
 * nothing here is cacheable and no authentication challenge is offered, so a
 * throttled request is indistinguishable from a plain refusal.
 */
export function tooManyRequests(method: string): Response {
  return new Response(method === 'HEAD' ? null : TOO_MANY_REQUESTS_BODY, {
    status: 429,
    headers: {
      'Content-Type': GENERIC_CONTENT_TYPE,
      'Cache-Control': NO_STORE,
      'Retry-After': RETRY_AFTER_SECONDS,
      'X-Content-Type-Options': 'nosniff',
    },
  });
}

/** Builds a response for a resolved object. */
export function objectResponse(
  status: number,
  headers: Record<string, string>,
  body: ReadableStream<Uint8Array> | null,
): Response {
  return new Response(body, { status, headers });
}

/** Client-facing cache directive for a stored object. */
export function cacheControlFor(immutable: boolean): string {
  return immutable ? IMMUTABLE_CACHE_CONTROL : REVALIDATED_CACHE_CONTROL;
}

/** Headers common to every successful object response. */
export function objectHeaders(
  immutable: boolean,
  metadata: { etag: string; lastModified: Date | null; contentType: string | null },
): Record<string, string> {
  const headers: Record<string, string> = {
    'Cache-Control': cacheControlFor(immutable),
    'Accept-Ranges': 'bytes',
    ETag: metadata.etag,
  };
  if (metadata.lastModified !== null) {
    headers['Last-Modified'] = metadata.lastModified.toUTCString();
  }
  if (metadata.contentType !== null) headers['Content-Type'] = metadata.contentType;
  return headers;
}

/** Not modified; carries the validators and no payload. */
export function notModified(headers: Record<string, string>): Response {
  return objectResponse(304, headers, null);
}

/** Range cannot be satisfied; `Content-Range` advertises the current length. */
export function rangeNotSatisfiable(size: number, headers: Record<string, string>): Response {
  return objectResponse(416, { ...headers, 'Content-Range': `bytes */${size}` }, null);
}