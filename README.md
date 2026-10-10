# apt-at-home

Private Debian 13 APT repository with an authenticated Cloudflare Worker
gateway and R2 storage. English | [中文](README.zh-CN.md)

## What this is

- Authenticated Worker gateway (`worker/`): Basic Auth per-device tokens,
  `GET`/`HEAD` only, auth-first immutable caching with origin evaluation for
  `Range`/`If-*`, `current.json` release pointer with retained by-hash
  indices. Account `12412d5b061683494ea3e5c217d6a961`, bucket `apt`.
- Local package pipeline (`packaging/`, `scripts/`): pinned zellij 0.45.1 and
  starship 1.26.0 musl binaries for amd64/arm64, four deterministic `.deb`
  files (`-1` revision, `root/root`), signed APT metadata, fingerprint-pinned
  publication (`APT_SIGNING_FINGERPRINT`).
- Manual GitHub Actions releases plus a separate manual Worker deploy of
  the base config with `--domains`/`--secrets-file`, gated on
  `APT_WORKER_DOMAIN` and `CREDENTIALS`.
- Client config uses `Signed-By` and `auth.conf` (`chmod 600`); no `apt-key`
  or `trusted=yes`.

## Quick start (local only)

```sh
bun install
bun run typecheck
bun run test
uv sync
uv run python -m unittest discover -s tests
uv run python -m scripts.repo --help
```

Build and verify a signed tree (needs a local signing key file):

```sh
APT_SIGNING_KEY_FILE=/path/to/private.asc APT_SIGNING_KEY_ID=<fingerprint> \
  uv run python -m scripts.repo build --output dist/repo
APT_SIGNING_FINGERPRINT=<40-hex primary> uv run python -m scripts.repo verify --output dist/repo
```

See [docs/setup.md](docs/setup.md) for operator bucket, domain, secrets, and
token rotation, and [docs/publishing.md](docs/publishing.md) for the manual
release flow. Client placeholders live in `client/`.

## Repository layout

- `pool/*.deb`: immutable package files.
- `releases/<id>/dists/stable/...`: signed release (`Release`, `InRelease`,
  `Release.gpg`, per-arch `Packages`, `Packages.gz`).
- `indices/<arch>/SHA256/<digest>`: shared by-hash content, retained across
  pointer moves.
- `current.json`: selects the complete release. `key.asc`: repository key.

## Safety notes

- No web UI, upload API, or deployment runs from this checkout.
- Never overwrites immutable objects, never `--delete` syncs, pointer moves
  only after remote verification; production publish requires the full
  trusted fingerprint, even for key rotation.
- `Release` carries no `Valid-Until` (personal-repo tradeoff).
- A WAF or rate limit cannot guarantee blocking malicious Worker calls.
