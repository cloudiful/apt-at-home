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
- The base config carries no `routes` and is deployed as-is with
  `bunx wrangler deploy --config worker/wrangler.jsonc --domains "$APT_WORKER_DOMAIN"`.
  The domain comes from the validated `APT_WORKER_DOMAIN` repository
  variable; until the operator supplies that variable, deployment stays
  explicitly blocked. No generated config is used, so the deployed
  entrypoint always resolves.
- Point the private hostname at the Worker. The Worker is the only public
  ingress. Disable any direct bucket endpoint.

## 2. Worker credentials secret

- The Worker reads a `CREDENTIALS` secret: JSON mapping device names to
  SHA256 hex digests of per-device random tokens.
- Generate one random token per device locally, never commit it:
  `openssl rand -hex 32`.
- Compute digests locally and upload only the digest map:
  `python3 -c "import hashlib; print(hashlib.sha256(b'<token>').hexdigest())"`.
- Check credentials before any storage or cache access (the gateway already
  does this). Invalid configuration fails closed with `503`.
- Production deploy requires the `CREDENTIALS` secret plus
  `CLOUDFLARE_API_TOKEN`; the workflow validates the domain, both secrets,
  and the `CREDENTIALS` JSON schema (non-empty object mapping device names
  to 64-hex digests) before any deploy, then provisions the secret
  atomically with `--secrets-file` in the same deployment version. The
  generated secrets file is `chmod 600` and scrubbed afterwards, and the
  bucket is never exposed publicly.

## 3. GitHub environment `apt-production`

`release.yml` and `worker-deploy.yml` are `workflow_dispatch` only, carry
`environment: apt-production` on their jobs, serialize through a single
concurrency group, and use least-privilege `contents: read`. Workflow inputs
reach shell steps only through step `env` (never `${{ }}` interpolation).
The release keeps the imported key file until the final always-cleanup and
signs with `APT_SIGNING_KEY_ID` set to the full trusted fingerprint.
Configure these protected secrets:

- `APT_S3_BUCKET`, `APT_S3_ENDPOINT`, `APT_S3_REGION`,
  `APT_S3_ACCESS_KEY_ID`, `APT_S3_SECRET_ACCESS_KEY`
- `APT_RELEASE_SIGNING_KEY` (armored private key, manual releases only)
- `APT_SIGNING_FINGERPRINT` (full 40-hex primary fingerprint, enforced at
  verify and publish — not an echo)
- `CLOUDFLARE_API_TOKEN`, `CREDENTIALS` (Worker deploy only),
  `APT_WORKER_DOMAIN` variable (bare hostname, validated)
- Never expose PR secrets to releases; `ci.yml` uses an ephemeral CI key and
  never touches production credentials. No Worker deploy is mixed into the
  package-upload release.

## 4. Local credential and key bootstrap

- Copy `client/personal.sources.example` to
  `/etc/apt/sources.list.d/apt-at-home.sources`, replacing the hostname and
  installing the repository public key at the `Signed-By` path. Never use
  `apt-key` or `trusted=yes`.
- Copy `client/auth.conf.example` to `/etc/apt/auth.conf.d/apt-at-home.conf`,
  replacing the device name and token, then `chmod 600` the file. Tokens stay
  out of URLs, shell history, and logs.
- Verify the signing key by its full trusted primary fingerprint before
  installing it as `key.asc`:
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

## 5. WAF, limits, and alerts (limitations)

- A Worker-side WAF, rate limit, or alert cannot guarantee blocking of
  malicious invocations on the free tier; treat them as defense in depth.
- Keep tokens per-device and rotatable, watch Worker request logs for `401`
  bursts, and rotate any token that may have leaked.
- There is deliberately no `Valid-Until` on `Release`; clients do not expire
  metadata. This is a personal-repo freshness tradeoff documented in the
  manifest tooling.

## 6. Token rotation

1. Generate a new token for the device and add its digest to `CREDENTIALS`.
2. Update that device's `auth.conf` entry (`chmod 600`).
3. Confirm the device fetches through the Worker, then remove the old digest.
4. Never reuse a token across devices and never log tokens.
