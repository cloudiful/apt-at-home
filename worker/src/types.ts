/** Cloudflare Worker bindings required by the APT gateway. */
export interface Env {
  /** Private R2 bucket holding the published repository. Never a public bucket. */
  readonly BUCKET: R2Bucket;
  /** Worker secret: JSON object mapping client name to SHA256 hex digest of its token. */
  readonly CREDENTIALS: string;
  /**
   * Optional testing toggle as exact text: absent or `"true"` requires
   * credentials, only `"false"` allows anonymous artifact reads. Any other
   * value fails closed with `503`.
   */
  readonly AUTH_REQUIRED?: string;
}

/** Key of the object that selects the currently published release. */
export const POINTER_KEY = 'current.json';

/** Object key of the repository signing key. */
export const KEY_KEY = 'key.asc';

/** Architectures this gateway is allowed to serve. */
export const SUPPORTED_ARCHITECTURES = ['amd64', 'arm64'] as const;

export type Architecture = (typeof SUPPORTED_ARCHITECTURES)[number];

export function isSupportedArchitecture(value: string): value is Architecture {
  return (SUPPORTED_ARCHITECTURES as readonly string[]).includes(value);
}

/** Metadata read from a stored object, normalised away from the R2 surface. */
export interface StoredObject {
  readonly size: number;
  /** Strong entity tag derived from the stored content. */
  readonly etag: string;
  /** Whole-second last modification time, or null when the bucket reports none. */
  readonly lastModified: Date | null;
  /** Stored content type, or null when the object carries none. */
  readonly contentType: string | null;
}

/** A stored object whose payload is available for streaming. */
export interface StoredBody extends StoredObject {
  readonly body: ReadableStream<Uint8Array>;
}

/**
 * A client request resolved to exactly one readable object key.
 *
 * `immutable` marks content-addressed or write-once objects that may be cached
 * indefinitely; everything else is revalidated on every fetch because the
 * published release can change underneath it.
 */
export interface ResolvedObject {
  readonly key: string;
  readonly immutable: boolean;
}