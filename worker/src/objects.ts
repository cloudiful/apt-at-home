/**
 * Serving a resolved object out of R2 with HTTP-correct conditional and range
 * handling.
 *
 * Only the read side of the bucket binding is used: `head` for metadata and
 * `get` for the payload. Nothing here writes, lists or deletes.
 */
import {
  notFound,
  notModified,
  objectHeaders,
  objectResponse,
  rangeNotSatisfiable,
} from './responses.js';
import type { ByteRange } from './responses.js';
import type { Env, ResolvedObject, StoredObject } from './types.js';

/** How a validated request applies to one object. */
type Evaluation =
  | { readonly kind: 'not-modified' }
  | { readonly kind: 'unsatisfiable' }
  | { readonly kind: 'partial'; readonly range: ByteRange }
  | { readonly kind: 'full' };

export async function serveObject(
  env: Env,
  request: Request,
  resolved: ResolvedObject,
): Promise<Response> {
  const head = await env.BUCKET.head(resolved.key);
  if (head === null) return notFound();

  const metadata: StoredObject = {
    size: head.size,
    etag: normalizeEtag(head.httpEtag),
    lastModified: head.uploaded,
    contentType: head.httpMetadata?.contentType ?? null,
  };
  const headers = objectHeaders(resolved.immutable, metadata);
  const isHead = request.method === 'HEAD';

  const evaluation = evaluate(request, metadata);
  if (evaluation.kind === 'not-modified') return notModified(headers);
  if (evaluation.kind === 'unsatisfiable') {
    return rangeNotSatisfiable(metadata.size, headers);
  }

  if (evaluation.kind === 'partial') {
    const { offset, length } = evaluation.range;
    headers['Content-Range'] = `bytes ${offset}-${offset + length - 1}/${metadata.size}`;
    headers['Content-Length'] = String(length);
    if (isHead) return objectResponse(206, headers, null);
    const partial = await env.BUCKET.get(resolved.key, { range: { offset, length } });
    if (partial === null) return notFound();
    return objectResponse(206, headers, partial.body);
  }

  headers['Content-Length'] = String(metadata.size);
  if (isHead) return objectResponse(200, headers, null);
  const object = await env.BUCKET.get(resolved.key);
  if (object === null) return notFound();
  return objectResponse(200, headers, object.body);
}

/** Applies conditional requests before range selection, as RFC 9110 requires. */
function evaluate(request: Request, metadata: StoredObject): Evaluation {
  const ifNoneMatch = request.headers.get('if-none-match');
  if (ifNoneMatch !== null && entityTagMatches(ifNoneMatch, metadata.etag)) {
    return { kind: 'not-modified' };
  }

  const ifModifiedSince = request.headers.get('if-modified-since');
  if (ifNoneMatch === null && ifModifiedSince !== null) {
    const since = parseHttpDate(ifModifiedSince);
    const modified = metadata.lastModified;
    if (since !== null && modified !== null && toSeconds(modified) <= toSeconds(since)) {
      return { kind: 'not-modified' };
    }
  }

  const rangeHeader = request.headers.get('range');
  if (rangeHeader === null) return { kind: 'full' };

  const ifRange = request.headers.get('if-range');
  if (ifRange !== null && !ifRangeMatches(ifRange, metadata)) return { kind: 'full' };

  return parseByteRange(rangeHeader, metadata.size);
}

/** Weak comparison, as `If-None-Match` requires. */
function entityTagMatches(header: string, etag: string): boolean {
  for (const candidate of header.split(',')) {
    const value = candidate.trim();
    if (value === '*') return true;
    if (stripWeakPrefix(value) === stripWeakPrefix(etag)) return true;
  }
  return false;
}

/** Strong comparison for `If-Range`, falling back to a last-modified date. */
function ifRangeMatches(header: string, metadata: StoredObject): boolean {
  const value = header.trim();
  if (value.startsWith('"') || value.startsWith('W/')) {
    return !value.startsWith('W/') && value === metadata.etag;
  }
  const since = parseHttpDate(value);
  const modified = metadata.lastModified;
  return since !== null && modified !== null && toSeconds(modified) <= toSeconds(since);
}

/**
 * Parses a single byte range. Anything malformed or multipart is ignored and
 * the whole object is served; a syntactically valid but unsatisfiable range is
 * reported so the client learns the real length.
 */
function parseByteRange(header: string, size: number): Evaluation {
  const unit = /^bytes=(.*)$/i.exec(header.trim());
  if (unit === null) return { kind: 'full' };
  const specs = (unit[1] ?? '').split(',');
  if (specs.length !== 1) return { kind: 'full' };
  const spec = (specs[0] ?? '').trim();
  if (spec.length === 0) return { kind: 'full' };

  const bounds = /^(\d*)-(\d*)$/.exec(spec);
  if (bounds === null) return { kind: 'full' };
  const [, rawStart = '', rawEnd = ''] = bounds;
  if (rawStart.length === 0 && rawEnd.length === 0) return { kind: 'full' };
  if (size === 0) return { kind: 'unsatisfiable' };

  if (rawStart.length === 0) {
    const suffix = Number(rawEnd);
    if (suffix === 0) return { kind: 'unsatisfiable' };
    const offset = Math.max(0, size - suffix);
    return { kind: 'partial', range: { offset, length: size - offset } };
  }

  const offset = Number(rawStart);
  if (!Number.isSafeInteger(offset) || offset >= size) return { kind: 'unsatisfiable' };
  if (rawEnd.length === 0) {
    return { kind: 'partial', range: { offset, length: size - offset } };
  }
  const last = Number(rawEnd);
  if (!Number.isSafeInteger(last) || last < offset) return { kind: 'unsatisfiable' };
  const end = Math.min(last, size - 1);
  return { kind: 'partial', range: { offset, length: end - offset + 1 } };
}

/** Normalises the entity tag R2 reports into a strong quoted validator. */
function normalizeEtag(raw: string): string {
  const bare = raw.replace(/^W\//, '').replace(/^"|"$/g, '');
  return `"${bare}"`;
}

function stripWeakPrefix(value: string): string {
  return value.startsWith('W/') ? value.slice(2) : value;
}

function toSeconds(date: Date): number {
  return Math.floor(date.getTime() / 1000);
}

/** Parses an IMF-fixdate, the only format a client may send for these headers. */
function parseHttpDate(value: string): Date | null {
  const parsed = Date.parse(value.trim());
  return Number.isNaN(parsed) ? null : new Date(parsed);
}