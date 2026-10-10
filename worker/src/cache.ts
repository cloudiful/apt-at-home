/**
 * Optional server-side caching of immutable repository objects.
 *
 * Only the caller decides eligibility: this module is reached after
 * authentication and only for content-addressed or write-once keys. The cache
 * key is derived from the object key alone, so no credential can ever end up in
 * a stored request, and the cached copy is returned with the private cache
 * directive the client must see.
 */

const CACHE_ORIGIN = 'https://cache.invalid';
const INTERNAL_CACHE_CONTROL = 'public, max-age=31536000';

type CacheHost = { readonly caches?: { readonly default?: Cache } };

/** Returns the runtime cache, or `undefined` where the runtime has none. */
function defaultCache(): Cache | undefined {
  return (globalThis as CacheHost).caches?.default;
}

/**
 * Cache URL for one object key.
 *
 * The key is split and re-encoded so the path structure of the object key can
 * never leak into, or be reshaped by, the URL.
 */
function cacheUrlFor(key: string): string {
  return `${CACHE_ORIGIN}/${key.split('/').map(encodeURIComponent).join('/')}`;
}

/** Reads a cached body, or `null` on a miss or when caching is unavailable. */
export async function cacheLookup(key: string): Promise<Response | null> {
  const cache = defaultCache();
  if (cache === undefined) return null;
  const cached = await cache.match(new Request(cacheUrlFor(key)));
  return cached ?? null;
}

/**
 * Stores a full, unpartial body for later reuse. Partial responses are never
 * stored, and the store request carries no headers from the client.
 */
export async function cacheStore(key: string, response: Response): Promise<void> {
  const cache = defaultCache();
  if (cache === undefined) return;
  const headers = new Headers(response.headers);
  headers.set('Cache-Control', INTERNAL_CACHE_CONTROL);
  const stored = new Response(response.body, { status: 200, headers });
  try {
    await cache.put(new Request(cacheUrlFor(key)), stored);
  } catch {
    // A cache write failure must never fail the request it was serving.
  }
}

/** Re-applies the client-facing cache directive to a cache hit. */
export function toClientResponse(cached: Response, cacheControl: string): Response {
  const headers = new Headers(cached.headers);
  headers.set('Cache-Control', cacheControl);
  return new Response(cached.body, { status: cached.status, headers });
}

/** True when a response may be stored: full length, no range, and no errors. */
export function isCacheableBody(response: Response): boolean {
  return response.status === 200 && response.headers.get('Content-Range') === null;
}

/**
 * True when a request must bypass the cache read and go to the origin
 * evaluator: any Range or conditional header needs origin evaluation for
 * correct 206/304 semantics. Authentication still runs first; this only
 * decides whether a cache hit may short-circuit `serveObject()`.
 */
export function shouldBypassCache(request: Request): boolean {
  const headers = request.headers;
  if (headers.get('range') !== null) return true;
  if (headers.get('if-none-match') !== null) return true;
  if (headers.get('if-modified-since') !== null) return true;
  if (headers.get('if-range') !== null) return true;
  if (headers.get('if-match') !== null) return true;
  if (headers.get('if-unmodified-since') !== null) return true;
  return false;
}