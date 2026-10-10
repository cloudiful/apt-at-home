# Publishing

Manual releases only. There is no scheduled publish and no automatic `latest`
tracking. Concurrent publishers are not supported: `release.yml` serializes
through `concurrency: apt-release`, and operators must not run two manual
publishes at once.

## Local build

```sh
uv sync
uv run python -m unittest discover -s tests
uv run python -m scripts.repo --help
APT_SIGNING_KEY_FILE=/path/to/private.asc APT_SIGNING_KEY_ID=<fingerprint> \
  uv run python -m scripts.repo build --output dist/repo
uv run python -m scripts.repo verify --output dist/repo
```

- `build` downloads the pinned musl archives in `packaging/manifest.json`,
  verifies SHA256 before extraction, extracts without executing upstream
  binaries, assembles four `.deb` files (`-1` revision, explicit `xz`
  compression, no maintainer scripts, `root/root` ownership, deterministic
  `SOURCE_DATE_EPOCH`), generates `Packages` with `apt-ftparchive`, deterministic
  `Packages.gz` (`gzip -n`), a `Release` with `Suite/Codename stable`,
  `Architectures amd64 arm64`, `Acquire-By-Hash yes`, and no `Valid-Until`,
  plus shared `indices/<arch>/SHA256/<digest>` copies, then signs
  `InRelease`/`Release.gpg` in an isolated `GNUPGHOME` and writes
  `current.json` and `key.asc`.
- Credentials never appear in CLI arguments; the signing key comes from
  `APT_SIGNING_KEY_FILE` (path only) and S3 credentials from the environment.
- Building and signing need full GnuPG (`gnupg`, which provides `gpg-agent`);
  a bare `gpg` binary cannot generate or import keys non-interactively.

## Dependency locks

Both locks stay reproducible against public sources, so any CI or Cloudflare
build works without a private mirror and without upgrading anything:

- `uv.lock` pins the exact resolved versions and artifact hashes.
  `uv lock --check --default-index https://pypi.org/simple` fails if the lock
  drifts from `pyproject.toml` or stops resolving from public PyPI. Regenerate
  in place with `uv lock --default-index https://pypi.org/simple`; never
  upgrade or delete entries in the same change.
- `package-lock.json` stays reproducible under `npm ci`. `package.json`
  approves the only two install scripts the lock needs through a
  version-pinned `allowScripts` map; never widen it to a name-only or
  wildcard approval.

## Verify

`verify` checks the pointer, the `Release` headers and `SHA256`/`SHA512`
entries against local `Packages` bytes, `Packages` references against `pool`
hashes, by-hash index equality, and both signatures in a throwaway homedir.
It fails on any missing file, hash mismatch, or bad signature. When
`APT_SIGNING_FINGERPRINT` is set, it additionally requires `key.asc` to be
the single primary key matching that full 40-hex fingerprint (whitespace and
case are ignored; short IDs are rejected):

```sh
APT_SIGNING_FINGERPRINT=<40-hex primary> uv run python -m scripts.repo verify --output dist/repo
```

## Publish

```sh
uv run python -m scripts.repo publish --output dist/repo --bucket <name>
```

- Requires `APT_SIGNING_FINGERPRINT` (full primary) and a verified tree.
  A missing, malformed, or mismatched pin fails before any object is
  uploaded, so wrong-key publishes leave zero writes. Uploads `pool`,
  `indices`, the full release, and `key.asc`, re-verifies remote hashes,
  then writes `current.json` last.
- Never deletes: old `pool` files and `indices` entries are retained so
  previous by-hash URLs keep working after a pointer move.
- Never overwrites immutable bytes with different content; a colliding
  `Release` id or pool object aborts before the pointer moves.
- `key.asc` replacement needs `--allow-key-rotation` with operator
  confirmation.
- The S3 endpoint and credentials come from `APT_S3_*`/`AWS_*` environment
  variables, never from CLI secrets or URLs.
- The GitHub `release.yml` publish job runs manual-only in the `prod`
  environment with the R2 and signing secrets. Published artifacts stay behind
  the credential gate by default; testing without credentials uses the runtime
  `AUTH_REQUIRED` text toggle (`"false"` exposes downloads, `"true"` or
  deleted re-enables auth — see setup).
