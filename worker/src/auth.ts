/**
 * Fail-closed HTTP Basic authentication for per-device repository tokens.
 *
 * The worker never holds a plaintext token: the secret holds one SHA256 hex
 * digest per client name, and the presented token is hashed and compared in
 * constant work so neither a wrong token nor an unknown name is distinguishable
 * by timing or by the shape of the response.
 */

/** Longest `Authorization` header accepted before parsing, to bound work. */
const MAX_HEADER_LENGTH = 4096;
/** Longest `user:token` payload accepted after base64 decoding. */
const MAX_CREDENTIAL_LENGTH = 512;
/** Longest accepted token. */
const MAX_TOKEN_LENGTH = 256;
/** Realm advertised in the challenge so apt retries with Basic credentials. */
const REALM = 'apt-at-home';

const USERNAME_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;
/** Visible ASCII without space: no whitespace, no control characters, no Unicode. */
const TOKEN_PATTERN = /^[\x21-\x7e]{1,256}$/;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/;

/**
 * Comparison target for an unknown client name. A valid digest always has 64 hex
 * characters, so an unknown name still performs the full comparison.
 */
const ABSENT_DIGEST = '0'.repeat(64);

export type CredentialMap = ReadonlyMap<string, string>;

/** Parsed secret configuration; `null` means the secret is unusable. */
export type ConfigResult =
  | { readonly ok: true; readonly credentials: CredentialMap }
  | { readonly ok: false };

/**
 * Result of resolving the optional `AUTH_REQUIRED` runtime variable.
 *
 * `required` keeps the fail-closed credential gate; `public` allows anonymous
 * artifact reads for testing; an `ok: false` result means the configured value
 * is malformed and the gateway must fail closed with `503`.
 */
export type AuthModeResult =
  | { readonly ok: true; readonly mode: 'required' | 'public' }
  | { readonly ok: false };

/**
 * Resolves the optional text `AUTH_REQUIRED` variable without coercion.
 *
 * Absent (`undefined`) or the exact lowercase text `"true"` requires
 * credentials as before. Only the exact lowercase text `"false"` disables the
 * credential gate. Every other value — including the empty string, whitespace,
 * case variants, booleans, and null — is malformed. The input is never trimmed
 * or coerced so testing stays explicit and the default stays private.
 */
export function resolveAuthMode(raw: unknown): AuthModeResult {
  if (raw === undefined) return { ok: true, mode: 'required' };
  if (raw === 'true') return { ok: true, mode: 'required' };
  if (raw === 'false') return { ok: true, mode: 'public' };
  return { ok: false };
}

/** Parses the credential secret, rejecting anything the worker cannot use safely. */
export function parseCredentialConfig(raw: unknown): ConfigResult {
  if (typeof raw !== 'string') return { ok: false };
  const trimmed = raw.trim();
  if (trimmed.length === 0) return { ok: false };

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { ok: false };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false };
  }

  // A Map, not an object: a name such as `constructor` or `toString` must never
  // resolve through the prototype chain.
  const credentials = new Map<string, string>();
  for (const [name, digest] of Object.entries(parsed)) {
    if (!isValidUsername(name) || !isValidDigest(digest)) return { ok: false };
    credentials.set(name, digest.toLowerCase());
  }
  if (credentials.size === 0) return { ok: false };
  return { ok: true, credentials };
}

function isValidUsername(name: string): boolean {
  return USERNAME_PATTERN.test(name);
}

function isValidDigest(digest: unknown): digest is string {
  return typeof digest === 'string' && DIGEST_PATTERN.test(digest.toLowerCase());
}

/** Challenge headers returned with every rejection so apt can send credentials. */
export function challengeHeaders(): Record<string, string> {
  return {
    'WWW-Authenticate': `Basic realm="${REALM}", charset="UTF-8"`,
    'Cache-Control': 'no-store',
  };
}

/**
 * Verifies an `Authorization` header against the credential map.
 *
 * Every rejection path performs the same bounded work and yields the same
 * result, so a caller cannot distinguish a bad scheme from a bad token.
 */
export async function isAuthorized(
  header: string | null,
  credentials: CredentialMap,
): Promise<boolean> {
  if (header === null || header.length === 0 || header.length > MAX_HEADER_LENGTH) {
    return false;
  }
  const separator = header.indexOf(' ');
  if (separator < 0) return false;
  if (header.slice(0, separator).toLowerCase() !== 'basic') return false;

  const encoded = header.slice(separator + 1);
  if (encoded.length === 0) return false;

  let decoded: string;
  try {
    decoded = atob(encoded);
  } catch {
    return false;
  }
  if (decoded.length === 0 || decoded.length > MAX_CREDENTIAL_LENGTH) return false;

  const colon = decoded.indexOf(':');
  if (colon < 0) return false;
  const username = decoded.slice(0, colon);
  const token = decoded.slice(colon + 1);
  if (!isValidUsername(username) || !isValidToken(token)) return false;

  const expected = credentials.get(username) ?? ABSENT_DIGEST;
  return constantTimeEqual(await sha256Hex(token), expected);
}

function isValidToken(token: string): boolean {
  return token.length <= MAX_TOKEN_LENGTH && TOKEN_PATTERN.test(token);
}

/** Compares two equal-length hex digests without an early exit. */
function constantTimeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const view = new Uint8Array(digest);
  let hex = '';
  for (const byte of view) hex += byte.toString(16).padStart(2, '0');
  return hex;
}