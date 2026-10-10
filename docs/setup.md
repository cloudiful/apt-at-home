# Operator setup

Configure this repository for the operator's own account and storage in
`worker/wrangler.jsonc` (`account_id`, `bucket_name`). Keep the bucket
private with no `r2.dev` access and no bucket custom domain. No cloud
provisioning happens from this checkout. The operator creates the custom
domain, the Worker secret, and the GitHub environment by hand. No live deploy
or bucket mutation is performed here.

## 1. Private bucket and domain

- Create the operator's private bucket and set `account_id` and `bucket_name`
  in `worker/wrangler.jsonc`; keep `workers_dev: false` and
  `preview_urls: false`. Keep the bucket private: no public `r2.dev` access
  and no bucket custom domain.
- The base config carries no `routes`. The Worker custom domain is bound by the
  deploy command (section 3) rather than committed to the config, so the
  deployed entrypoint always resolves.
- Point the private hostname at the Worker. The Worker is the only public
  ingress. Disable any direct bucket endpoint.

## 2. Worker runtime credentials

- The Worker reads a `CREDENTIALS` secret: JSON mapping device names to
  SHA256 hex digests of per-device random tokens.
- Generate one random token per device locally, never commit it:
  `openssl rand -hex 32`.
- Compute digests locally and upload only the digest map:
  `python3 -c "import hashlib; print(hashlib.sha256(b'<token>').hexdigest())"`.
- Store `CREDENTIALS` as a **runtime** secret in the Cloudflare dashboard
  (Worker → Settings → Variables and Secrets). The gateway reads it while
  serving requests; the build never does.
- Optional testing toggle `AUTH_REQUIRED` is exact text in Variables and
  Secrets (a runtime variable, not a build variable and never committed):
  absent or `"true"` keeps the credential gate, only `"false"` allows
  anonymous artifact reads. Any other value fails closed with `503` before
  storage or cache, except `/` which stays public. `"false"` exposes downloads
  to anyone who can reach the Worker, so set `"true"` or delete the variable
  immediately after testing. `keep_vars: true` in `worker/wrangler.jsonc` keeps
  dashboard plaintext variables across deployments; secrets are unaffected.
- Check credentials before any storage or cache access (the gateway already
  does this). Invalid configuration fails closed with `503`, and the bucket is
  never exposed publicly.
- The exact root path `/` is the one public exception: `GET`/`HEAD /` serves a
  static installation guide before the credential check, so a misconfigured
  runtime still explains how to bootstrap a client. The guide follows the
  browser's `Accept-Language` (English by default), and `?lang=en` or `?lang=zh`
  overrides it. Every other path stays
  behind the credential gate, and a `503` there means the runtime credentials
  secret is missing or unusable — fix it under Worker → Settings → Variables
  and Secrets. The guide only installs anything after the first successful
  release publishes the repository.

## 3. Cloudflare Worker Builds (deploy owner)

Cloudflare builds and deploys the gateway straight from this GitHub
repository, so no GitHub workflow deploys the Worker.

- In the Cloudflare dashboard, create the Worker and connect its Git
  integration to this repository. Use the repository root as the build root.
- Set the build variable `SKIP_DEPENDENCY_INSTALL=1`. This supported switch
  stops the automatic Python/uv dependency install, so the build stays
  JavaScript-only and never resolves a Python index.
- Build command: `npm ci && npm run typecheck && npm test`.
- Deploy command:
  `npx wrangler deploy --config worker/wrangler.jsonc --domains apt.example.com`
  (replace the example hostname with the operator's own domain).
- Disable preview URLs so only the production hostname is served.

`package.json` keeps a pinned `allowScripts` map for the only two install
scripts the lock needs (`esbuild` and `workerd`). Nothing is approved by name
or wildcard. `ci.yml` still bundles the Worker with a Wrangler `--dry-run` and
synthetic credentials as a CI check; it never uploads.

## 4. GitHub environment `prod`

`release.yml` is `workflow_dispatch` only, carries `environment: prod`
on its publish job, serializes through `concurrency: apt-release`, and uses
least-privilege `contents: read`. Workflow inputs reach shell steps only
through step `env` (never `${{ }}` interpolation). The release keeps the
imported key file until the final always-cleanup and signs with
`APT_SIGNING_KEY_ID` set to the full trusted fingerprint. Configure these
protected secrets:

- `APT_S3_BUCKET`, `APT_S3_ENDPOINT`, `APT_S3_REGION`,
  `APT_S3_ACCESS_KEY_ID`, `APT_S3_SECRET_ACCESS_KEY`
- `APT_RELEASE_SIGNING_KEY` (armored private key, manual releases only)
- `APT_SIGNING_FINGERPRINT` (full 40-hex primary fingerprint, enforced at
  verify and publish — not an echo)

`ci.yml` runs the package build, verification, and native amd64/arm64 smoke on
every push, and pins `UV_DEFAULT_INDEX` to public PyPI so the committed lock
stays portable. It needs no cloud secrets: the CI signing key is ephemeral and
the Wrangler step is a dry run. Package publication and Worker deploys have
separate owners, so the GitHub environment holds only the R2 and GPG material.

## 5. Local credential and key bootstrap

- Copy `client/personal.sources.example` to
  `/etc/apt/sources.list.d/apt-at-home.sources`, replacing the hostname and
  installing the repository public key at the `Signed-By` path. The path must
  name that installed key file exactly. Never use `apt-key` or `trusted=yes`.
- Copy `client/auth.conf.example` to `/etc/apt/auth.conf.d/apt-at-home.conf`,
  replacing the device name and token, then `chmod 600` the file. Obtain the
  per-device username and token from the administrator out of band; tokens stay
  out of URLs, shell history, and logs.
- Verify the signing key by its full trusted primary fingerprint before
  installing it as `key.asc`, comparing it against the fingerprint received
  through a separate channel — never a fingerprint printed by the gateway:
  `APT_SIGNING_FINGERPRINT=<40-hex> uv run python -m scripts.repo verify --output dist/repo`.
  Local ephemeral builds may verify without the variable, but production
  verify and every publish require it; a missing, malformed, or mismatched
  pin fails before any upload, pointer, or key mutation — even with
  `--allow-key-rotation`. Rotation succeeds only when the replacement key
  matches the explicitly updated pin.
- Checksum provenance: tarball SHA256 values in `packaging/manifest.json`
  were verified by direct download against the GitHub API digests. Zellij
  upstream `.sha256sum` files cover the inner binary
  (`target/<triple>/release/zellij`), not the tarball; starship
  `.tar.gz.sha256` files cover the tarball. See `checksum_provenance` in the
  manifest.

## 6. WAF, limits, and alerts (limitations)

- A Worker-side WAF, rate limit, or alert cannot guarantee blocking of
  malicious invocations on the free tier; treat them as defense in depth.
- Keep tokens per-device and rotatable, watch Worker request logs for `401`
  bursts, and rotate any token that may have leaked.
- There is deliberately no `Valid-Until` on `Release`; clients do not expire
  metadata. This is a personal-repo freshness tradeoff documented in the
  manifest tooling.

## 7. Token rotation

1. Generate a new token for the device and add its digest to `CREDENTIALS`.
2. Update that device's `auth.conf` entry (`chmod 600`).
3. Confirm the device fetches through the Worker, then remove the old digest.
4. Never reuse a token across devices and never log tokens.
