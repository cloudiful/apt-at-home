# Publishing

One production workflow, `publish.yml`, owns every repository publication and
serializes on `concurrency: apt-production-release` with
`cancel-in-progress: false`, so an automatic update and a manual release can
never move the release pointer at the same time. It has two sources:

- The six-hourly schedule (`cron: "17 */6 * * *"`) always runs the **upstream**
  source: it restores the published release, compares it against the latest
  stable upstream releases, and publishes only when upstream moved. An unchanged
  run builds, signs, and uploads nothing and still succeeds.
- `workflow_dispatch` chooses `source` (`upstream` default, or `manifest`) and
  `dry_run` (`true` default). The upstream source plans with `--dry-run` unless
  `dry_run` is `false`, then runs `update --publish`. The manifest source builds
  the committed `packaging/manifest.json`: a preview builds it unsigned and
  checks the pool without the signing key, and a publish builds with the key,
  verifies, and publishes. `release_id` optionally names the release, and
  `allow_key_rotation` is accepted only for a manual manifest publish with
  `dry_run: false`.

The job is `environment: prod`, so every run waits for the same approvals, and it
publishes only from `main`. Inputs reach shell steps only through step `env`
(never `${{ }}` interpolation); a scheduled run ignores any dispatch input, and
an unsupported event or an out-of-range `source`/`dry_run`/`allow_key_rotation`
value fails closed before anything is mutated.

Manual runs (Actions → publish → Run workflow):

- Plan the next upstream update: `source: upstream`, `dry_run: true`.
- Publish the latest upstream releases: `source: upstream`, `dry_run: false`.
- Preview the committed manifest without the signing key: `source: manifest`,
  `dry_run: true`.
- Publish the committed manifest: `source: manifest`, `dry_run: false` (add
  `allow_key_rotation: true` only to rotate the trust anchor).

The signing key is materialized only when a run will actually publish: a
scheduled run, an upstream publish, or a manual manifest publish. The private key
is written to `$RUNNER_TEMP` with mode `600`, imported into an isolated
`GNUPGHOME`, and deleted by an always-run cleanup step; it is never an artifact
and never logged. Previews still need the public `APT_SIGNING_FINGERPRINT` pin
and the read-only storage credentials to restore and compare the published
baseline, but never the private key. Every path reuses the same seven `prod`
secrets; there are no Cloudflare secrets in the publication path.

## Local build

```sh
uv sync
uv run python -m unittest discover -s tests
uv run python -m scripts.repo --help
APT_SIGNING_KEY_FILE=/path/to/private.asc APT_SIGNING_KEY_ID=<fingerprint> \
  uv run python -m scripts.repo build --output dist/repo
uv run python -m scripts.repo verify --output dist/repo
```

- `build` downloads the pinned archives in `packaging/manifest.json`,
  verifies SHA256 before extraction, extracts one declared archive member
  without executing upstream binaries, assembles one `.deb` per package and
  architecture (`-1` revision, explicit `xz` compression, no maintainer
  scripts, `root/root` ownership, deterministic `SOURCE_DATE_EPOCH`),
  generates `Packages` from a throwaway `reprepro`
  database with flat `pool/` filenames, deterministic `Packages.gz`
  (`gzip -n`), a `Release` with `Suite/Codename stable`,
  `Architectures amd64 arm64`, `Acquire-By-Hash yes`, and no `Valid-Until`,
  plus shared `indices/<arch>/SHA256/<digest>` copies, then signs
  `InRelease`/`Release.gpg` in an isolated `GNUPGHOME` and writes
  `current.json` and `key.asc`.
- The manifest is the inventory: `python -m scripts.smoke --manifest
  packaging/manifest.json --count` prints how many `.deb` files a release of it
  contains, and `--pool DIR` checks a built pool against it. Never hard-code a
  package count in a workflow.
- `build` also writes `releases/<id>/manifest.json`, the canonical copy of the
  manifest it built from, and binds its SHA256 and size into the signed
  `Release` as `X-Manifest-SHA256` and `X-Manifest-Size`. `verify` requires both
  fields and recomputes them, so the stored copy can never stand in for the
  manifest that was actually built.
- System prerequisites for `build`: `reprepro` generates the `Packages`
  indices and `dpkg-deb` (shipped by `dpkg`) assembles the packages.
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

## Dependency graph

GitHub's dependency graph is independent of this publication path: GitHub builds
it automatically from the committed `uv.lock` and `package-lock.json`, so it
needs no workflow, no CI job, and no repository file. `publish.yml` and CI never
submit dependencies and never touch the graph; it stays a separate supply-chain
view.

## Verify

`verify` checks the pointer, the `Release` headers and `SHA256`/`SHA512`
entries against local `Packages` bytes, `Packages` references against `pool`
hashes, by-hash index equality, the bound baseline manifest digest and size,
and both signatures in a throwaway homedir. It fails on any missing file, hash
mismatch, or bad signature. When
`APT_SIGNING_FINGERPRINT` is set, it additionally requires `key.asc` to be
the single primary key matching that full 40-hex fingerprint (whitespace and
case are ignored; short IDs are rejected):

```sh
APT_SIGNING_FINGERPRINT=<40-hex primary> uv run python -m scripts.repo verify --output dist/repo
```

## Automatic updates

```sh
# Plan only: no key import, no build, no upload.
APT_SIGNING_FINGERPRINT=<40-hex> uv run python -m scripts.update --dry-run

# Build, sign, and publish when the plan is not empty.
APT_SIGNING_FINGERPRINT=<40-hex> \
  uv run python -m scripts.update --publish --bucket <name>
```

- The updater restores the published release through the full trust chain
  (pointer, pinned key, both signatures, clearsigned payload equality, signed
  `Packages` hashes, and the manifest bound by `X-Manifest-SHA256` /
  `X-Manifest-Size`) before it reads a single package byte, then compares that
  signed baseline against the latest stable upstream release of every package
  in the catalog: git-delta, neovim, rustfs-cli, sccache, starship, and zellij.
- Each package has one reviewed recipe: the official repository, the release
  tag prefix, the asset name, the archive member, the installed command, the
  declared dependencies, and the SPDX license. A version bump recomputes the
  member path from the new version, so an archive that nests its binary under a
  versioned directory never reuses a stale prefix.
- A package the published release does not carry yet is added from the catalog
  rather than replacing anything. Adding three packages to a two-package
  release builds six new `.deb` files and keeps the four published ones
  byte-identical; the final indices list every package the new bound manifest
  declares. The published version is authoritative, so a catalog that pins an
  older version never overwrites a newer published one.
- Only changed packages are rebuilt — one package is two architectures, so a
  later single-package update produces exactly two new `.deb` files while the
  rest of the pool stays byte-identical.
- A no-op is a success: nothing is built, signed, uploaded, or pointed at, and
  a second run against the new baseline finds nothing left to do.
- Versions only move forward. A downgrade, a missing architecture asset, a
  missing GitHub API digest, or a non-exact stable tag stops the run.

### When an update stops

- **License bytes changed.** The tagged `LICENSE` must equal the vendored copy;
  a difference stops the run for human review rather than assuming the change
  is acceptable.
- **Same version, different digest.** A republished tag is treated as a mutable
  release, not an unchanged one, and the run stops.
- **The published release does not bind a valid manifest.** A release whose
  `Release` omits `X-Manifest-*`, or whose bound manifest is missing or does not
  match the signed digest and size, stops the run before any package is read.
  Publish one release from the current `main` to repair the baseline.
- **Storage unreachable or denied.** Only a confirmed missing `current.json`
  means "no release yet"; a permission or network error stops the run instead of
  looking like empty storage.

## Publish

```sh
uv run python -m scripts.repo publish --output dist/repo --bucket <name>
```

- Requires `APT_SIGNING_FINGERPRINT` (full primary) and a verified tree.
  A missing, malformed, or mismatched pin fails before any object is
  uploaded, so wrong-key publishes leave zero writes. Uploads `pool`,
  `indices`, the full release, and `key.asc`, re-verifies remote hashes,
  then writes `current.json` last.
- A manual release is checked against the current published baseline before any
  upload: if the committed manifest is older than what clients already have,
  the publish is refused with an actionable error. Publish a manifest at least
  as new as the current release, or let the upstream source of `publish.yml`
  publish the newer one.
- Never deletes: old `pool` files and `indices` entries are retained so
  previous by-hash URLs keep working after a pointer move.
- Never overwrites immutable bytes with different content; a colliding
  `Release` id or pool object aborts before the pointer moves.
- `key.asc` replacement needs `--allow-key-rotation` with operator
  confirmation.
- The S3 endpoint and credentials come from `APT_S3_*`/`AWS_*` environment
  variables, never from CLI secrets or URLs.
- The GitHub `publish.yml` job runs in the `prod` environment with the R2 and
  signing secrets, and its single concurrency group serializes every
  publication. Published artifacts are public
  while the repository is in test mode: `worker/wrangler.jsonc` commits
  `AUTH_REQUIRED: "false"`, and that committed config is the source of truth on
  deploy. Restore the credential gate by setting it to `"true"` (or removing the
  `vars` entry) and deploying with a valid runtime `CREDENTIALS` — see setup.
