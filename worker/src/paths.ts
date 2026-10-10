/**
 * Strict mapping from a client request path to a single object key.
 *
 * Only three shapes are servable: a write-once package file, the distribution
 * tree of the currently published release, and a by-hash index entry. Every
 * other path, including anything that needs percent-decoding to become a
 * traversal, is rejected before any storage access.
 */
import { isSupportedArchitecture, KEY_KEY } from './types.js';
import type { ResolvedObject } from './types.js';

/** Files apt fetches directly under `dists/stable/`. */
const TOP_LEVEL_FILES = new Set(['Release', 'InRelease', 'Release.gpg']);
/** Per-architecture metadata files apt fetches from `dists/stable/main/binary-<arch>/`. */
const ARCHIVE_FILES = new Set(['Release', 'Packages', 'Packages.gz']);
/** Package file names, e.g. `zellij_0.45.1-1_amd64.deb`. */
const PACKAGE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9+._-]{0,199}\.deb$/;
/** SHA256 hex digest as used in a by-hash path. */
const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
/** Release identifier accepted from the pointer object. */
const RELEASE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

const SUITE = 'stable';
const COMPONENT = 'main';
const HASH_ALGORITHM = 'SHA256';

/** A request path that passed validation but may still need the release pointer. */
export interface ClassifiedPath {
  readonly key: string;
  readonly immutable: boolean;
  /** Set when the key can only be built from the published release id. */
  readonly releaseRelative: boolean;
}

export type PointerResult =
  | { readonly ok: true; readonly releaseId: string }
  | { readonly ok: false; readonly reason: 'invalid' };

/**
 * Validates a raw request path and classifies it.
 *
 * Returns `null` for any path outside the servable set, including malformed
 * percent-encoding and encoded separators or traversal segments.
 */
export function classifyPath(rawPath: string): ClassifiedPath | null {
  if (!rawPath.startsWith('/')) return null;
  const segments = rawPath.split('/').slice(1);
  if (segments.length === 0) return null;

  const decoded: string[] = [];
  for (const segment of segments) {
    const value = decodeSegment(segment);
    if (value === null) return null;
    decoded.push(value);
  }

  const head = decoded[0] as string;
  const rest = decoded.slice(1);

  if (head === KEY_KEY && rest.length === 0) {
    return { key: KEY_KEY, immutable: false, releaseRelative: false };
  }
  if (head === 'pool' && rest.length === 1) {
    const file = rest[0] as string;
    if (!PACKAGE_PATTERN.test(file)) return null;
    return { key: `pool/${file}`, immutable: true, releaseRelative: false };
  }
  if (head === 'dists' && rest[0] === SUITE) {
    return classifyDistribution(rest.slice(1));
  }
  return null;
}

/**
 * Decodes one path segment, rejecting anything that could change the segment
 * structure: an empty segment, a decoded separator, `.`/`..`, or a NUL byte.
 */
function decodeSegment(segment: string): string | null {
  if (segment.length === 0) return null;
  let value: string;
  try {
    value = decodeURIComponent(segment);
  } catch {
    return null;
  }
  if (value.length === 0) return null;
  if (value === '.' || value === '..') return null;
  if (value.includes('/') || value.includes('\\') || value.includes('\0')) return null;
  return value;
}

function classifyDistribution(rest: string[]): ClassifiedPath | null {
  if (rest.length === 0) return null;

  if (rest.length === 1 && TOP_LEVEL_FILES.has(rest[0] as string)) {
    return { key: `dists/${SUITE}/${rest[0]}`, immutable: false, releaseRelative: true };
  }

  if (rest[0] !== COMPONENT) return null;
  const componentRest = rest.slice(1);
  if (componentRest.length === 0) return null;

  const binary = componentRest[0] as string;
  if (!binary.startsWith('binary-')) return null;
  const architecture = binary.slice('binary-'.length);
  if (!isSupportedArchitecture(architecture)) return null;
  const tail = componentRest.slice(1);

  // A by-hash entry names content by digest, so it resolves to the shared
  // content-addressed index and stays downloadable after the pointer moves.
  if (tail.length === 3 && tail[0] === 'by-hash' && tail[1] === HASH_ALGORITHM) {
    const digest = tail[2] as string;
    if (!DIGEST_PATTERN.test(digest)) return null;
    return {
      key: `indices/${architecture}/${HASH_ALGORITHM}/${digest}`,
      immutable: true,
      releaseRelative: false,
    };
  }

  if (tail.length === 1 && ARCHIVE_FILES.has(tail[0] as string)) {
    return {
      key: `dists/${SUITE}/${COMPONENT}/${binary}/${tail[0]}`,
      immutable: false,
      releaseRelative: true,
    };
  }
  return null;
}

/** Reads and validates the release pointer document. */
export function parsePointer(body: string): PointerResult {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return { ok: false, reason: 'invalid' };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, reason: 'invalid' };
  }
  const releaseId = (parsed as { release?: unknown }).release;
  if (typeof releaseId !== 'string' || !RELEASE_PATTERN.test(releaseId)) {
    return { ok: false, reason: 'invalid' };
  }
  return { ok: true, releaseId };
}

/** Applies a validated release id to a path that is relative to a release. */
export function resolveReleasePath(
  classified: ClassifiedPath,
  releaseId: string,
): ResolvedObject | null {
  if (!RELEASE_PATTERN.test(releaseId)) return null;
  return { key: `releases/${releaseId}/${classified.key}`, immutable: false };
}

/** Resolves a path that names its object directly, without the release pointer. */
export function resolveDirectPath(classified: ClassifiedPath): ResolvedObject {
  return { key: classified.key, immutable: classified.immutable };
}