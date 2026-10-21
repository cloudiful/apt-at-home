/**
 * Shared fixture for the gateway tests: a private bucket holding one published
 * release for both architectures, plus the pointer, by-hash indexes and a
 * package file.
 *
 * The published release carries a synthetic manifest, so the tests never
 * depend on the roster the repository happens to publish today.
 */
import gateway from '../src/index.js';
import { FakeBucket, FakeCache, FakeContext, createEnv, credentialSecret, sha256Hex } from './fakes.js';

export const ORIGIN = 'https://apt.example.test';
export const CLIENT = 'laptop';
export const TOKEN = 't0ken-with-punctuation.~';
export const OTHER_TOKEN = 'another-device-token';
export const RELEASE_ID = '20260102-030405';
export const NEXT_RELEASE_ID = '20260203-040506';
export const KEY_OBJECT = 'PUBLIC KEY BLOCK';

export const RELEASE_BODY = 'Suite: stable\nOrigin: apt-at-home\n';
export const AMD64_PACKAGES = 'Package: zellij\nArchitecture: amd64\nVersion: 0.45.1-1\n';
export const ARM64_PACKAGES = 'Package: starship\nArchitecture: arm64\nVersion: 1.26.0-1\n';
export const PACKAGE_BODY = 'debian-package-archive-bytes';

/** One synthetic package of a published release. */
export interface SyntheticPackage {
  readonly name: string;
  readonly version: string;
  readonly architectures: readonly ('amd64' | 'arm64')[];
}

/** The catalog the fixture release publishes. */
export const SYNTHETIC_PACKAGES: readonly SyntheticPackage[] = [
  { name: 'alpha', version: '1.2.3', architectures: ['amd64', 'arm64'] },
  { name: 'beta', version: '0.4.0', architectures: ['amd64'] },
  { name: 'gamma', version: '2.0.0', architectures: ['arm64'] },
];

/** The catalog the next published release carries. */
export const NEXT_RELEASE_PACKAGES: readonly SyntheticPackage[] = [
  { name: 'alpha', version: '2.0.0', architectures: ['amd64', 'arm64'] },
  { name: 'delta', version: '3.1.4', architectures: ['amd64', 'arm64'] },
];

/** Renders a release manifest document for the given packages. */
export function manifestJson(packages: readonly SyntheticPackage[]): string {
  return JSON.stringify({
    manifest_version: 1,
    packages: packages.map((pkg) => ({
      name: pkg.name,
      version: pkg.version,
      targets: Object.fromEntries(
        pkg.architectures.map((architecture) => [
          architecture,
          { url: 'https://example.invalid/release.tar.gz', sha256: '0'.repeat(64) },
        ]),
      ),
    })),
  });
}

/** Stores the manifest of one published release in the bucket. */
export function seedManifest(
  bucket: FakeBucket,
  releaseId: string,
  packages: readonly SyntheticPackage[],
): void {
  bucket.seed(`releases/${releaseId}/manifest.json`, manifestJson(packages), {
    contentType: 'application/json',
  });
}

export interface Fixture {
  readonly bucket: FakeBucket;
  readonly cache: FakeCache;
  readonly ctx: FakeContext;
  readonly env: ReturnType<typeof createEnv>;
  readonly amd64Digest: string;
  readonly arm64Digest: string;
  readonly byHashAmd64: string;
  readonly byHashArm64: string;
  readonly nextReleaseBody: string;
  call(request: Request): Promise<Response>;
}

export async function createFixture(): Promise<Fixture> {
  const bucket = new FakeBucket();
  const cache = new FakeCache();
  const ctx = new FakeContext();
  const env = createEnv(bucket, await credentialSecret({ [CLIENT]: TOKEN, phone: OTHER_TOKEN }));

  const amd64Digest = await sha256Hex(AMD64_PACKAGES);
  const arm64Digest = await sha256Hex(ARM64_PACKAGES);
  const nextReleaseBody = 'Suite: stable\nOrigin: apt-at-home\nVersion: 2\n';

  seedRelease(bucket, RELEASE_ID, RELEASE_BODY, AMD64_PACKAGES, ARM64_PACKAGES);
  seedIndexes(bucket, amd64Digest, arm64Digest);
  seedManifest(bucket, RELEASE_ID, SYNTHETIC_PACKAGES);
  seedManifest(bucket, NEXT_RELEASE_ID, NEXT_RELEASE_PACKAGES);
  bucket.seed('current.json', JSON.stringify({ release: RELEASE_ID }), {
    contentType: 'application/json',
  });

  const fixture: Fixture = {
    bucket,
    cache,
    ctx,
    env,
    amd64Digest,
    arm64Digest,
    byHashAmd64: `/dists/stable/main/binary-amd64/by-hash/SHA256/${amd64Digest}`,
    byHashArm64: `/dists/stable/main/binary-arm64/by-hash/SHA256/${arm64Digest}`,
    nextReleaseBody,
    async call(request: Request): Promise<Response> {
      const response = await gateway.fetch(request, fixture.env, ctx.asBinding());
      await ctx.settled();
      return response;
    },
  };
  return fixture;
}

function seedRelease(
  bucket: FakeBucket,
  releaseId: string,
  releaseBody: string,
  amd64: string,
  arm64: string,
): void {
  const prefix = `releases/${releaseId}/dists/stable`;
  bucket.seed(`${prefix}/Release`, releaseBody, { contentType: 'text/plain' });
  bucket.seed(`${prefix}/InRelease`, releaseBody, { contentType: 'text/plain' });
  bucket.seed(`${prefix}/Release.gpg`, 'SIGNATURE', { contentType: 'application/pgp-signature' });
  for (const [architecture, body] of [
    ['amd64', amd64],
    ['arm64', arm64],
  ] as const) {
    bucket.seed(`${prefix}/main/binary-${architecture}/Packages`, body, {
      contentType: 'text/plain',
    });
    bucket.seed(`${prefix}/main/binary-${architecture}/Packages.gz`, 'GZIPPED', {
      contentType: 'application/gzip',
    });
  }
}

function seedIndexes(bucket: FakeBucket, amd64Digest: string, arm64Digest: string): void {
  bucket.seed(`indices/amd64/SHA256/${amd64Digest}`, AMD64_PACKAGES, {
    contentType: 'text/plain',
  });
  bucket.seed(`indices/arm64/SHA256/${arm64Digest}`, ARM64_PACKAGES, {
    contentType: 'text/plain',
  });
  bucket.seed('pool/zellij_0.45.1-1_amd64.deb', PACKAGE_BODY, {
    contentType: 'application/vnd.debian.binary-package',
  });
  bucket.seed('key.asc', KEY_OBJECT, { contentType: 'application/pgp-keys' });
}