/**
 * Fetch composition for the private APT repository gateway.
 *
 * The order of operations is the security contract: the credential secret is
 * validated, then the request is authenticated, and only then may the release
 * pointer, the cache or the bucket be touched. A rejected request therefore
 * reveals nothing about whether an object exists. The exact root path is the
 * single public exception: it is answered before any of this runs, and it
 * reads only the published release metadata, never the credential secret.
 */
import { challengeHeaders, isAuthorized, parseCredentialConfig, resolveAuthMode } from './auth.js';
import { cacheLookup, cacheStore, isCacheableBody, shouldBypassCache, toClientResponse } from './cache.js';
import { handleRoot } from './homepage.js';
import { serveObject } from './objects.js';
import {
  classifyPath,
  parsePointer,
  resolveDirectPath,
  resolveReleasePath,
} from './paths.js';
import {
  cacheControlFor,
  methodNotAllowed,
  notFound,
  serviceUnavailable,
  unauthorized,
} from './responses.js';
import { POINTER_KEY } from './types.js';
import type { ClassifiedPath } from './paths.js';
import type { Env, ResolvedObject } from './types.js';

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const root = await handleRoot(request, env);
    if (root !== null) return root;

    const authMode = resolveAuthMode(env.AUTH_REQUIRED);
    if (!authMode.ok) return serviceUnavailable();

    if (authMode.mode === 'required') {
      const config = parseCredentialConfig(env.CREDENTIALS);
      if (!config.ok) return serviceUnavailable();

      if (!(await isAuthorized(request.headers.get('authorization'), config.credentials))) {
        return unauthorized(challengeHeaders());
      }
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return methodNotAllowed();
    }

    const classified = classifyPath(new URL(request.url).pathname);
    if (classified === null) return notFound();

    const target = await resolveTarget(env, classified);
    if (!target.ok) {
      return target.reason === 'absent' ? notFound() : serviceUnavailable();
    }
    const resolved = target.object;

    const cacheControl = cacheControlFor(resolved.immutable);
    const mayUseCache = resolved.immutable && request.method === 'GET';
    const bypassCache = shouldBypassCache(request);

    if (mayUseCache && !bypassCache) {
      const cached = await cacheLookup(resolved.key);
      if (cached !== null) return toClientResponse(cached, cacheControl);
    }

    const response = await serveObject(env, request, resolved);
    if (mayUseCache && isCacheableBody(response)) {
      ctx.waitUntil(cacheStore(resolved.key, response.clone()));
    }
    return response;
  },
};

/** A classified path resolved to a single object, or the reason it cannot be. */
type Target =
  | { readonly ok: true; readonly object: ResolvedObject }
  | { readonly ok: false; readonly reason: 'absent' | 'unavailable' };

/**
 * Turns a classified path into an object key.
 *
 * `absent` means nothing is published yet; `unavailable` means the release
 * pointer exists but does not select a complete release.
 */
async function resolveTarget(env: Env, classified: ClassifiedPath): Promise<Target> {
  if (!classified.releaseRelative) {
    return { ok: true, object: resolveDirectPath(classified) };
  }

  const pointer = await env.BUCKET.get(POINTER_KEY);
  if (pointer === null) return { ok: false, reason: 'absent' };
  const parsed = parsePointer(await pointer.text());
  if (!parsed.ok) return { ok: false, reason: 'unavailable' };

  const resolved = resolveReleasePath(classified, parsed.releaseId);
  return resolved === null
    ? { ok: false, reason: 'unavailable' }
    : { ok: true, object: resolved };
}