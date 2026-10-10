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
- The repository is deployed in **public test mode**: `worker/wrangler.jsonc`
  commits the non-secret variable `AUTH_REQUIRED: "false"`, which is exact text
  (a runtime variable, not a build variable). The committed config is the source
  of truth for this value and overrides the same key set in the dashboard on
  every deploy, so editing the dashboard alone does not change it. `keep_vars:
  true` still preserves every **other** dashboard plaintext variable across
  deployments; secrets are unaffected. Artifact downloads therefore need no
  credentials and anyone who can reach the Worker can fetch them.
- To restore the credential gate, change `AUTH_REQUIRED` to `"true"` (or remove
  the `vars` entry) in `worker/wrangler.jsonc`, set a valid runtime
  `CREDENTIALS` secret, and deploy. Absent or `"true"` requires credentials;
  only `"false"` allows anonymous artifact reads, and any other value fails
  closed with `503` before storage or cache, except `/` which stays public.
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

`publish.yml` is the single production entrypoint. Its six-hour schedule
(`cron: "17 */6 * * *"`) publishes upstream updates automatically, and
`workflow_dispatch` chooses `source` (`upstream` default, or `manifest`) and
`dry_run` (`true` default, so a manual run plans unless it explicitly
publishes). The publish job carries `environment: prod`, so a scheduled run
waits for the same approvals as a manual release rather than bypassing them, and
it serializes through `concurrency: apt-production-release` with
`cancel-in-progress: false`, so an automatic update and a manual release can
never move the release pointer at the same time. It uses least-privilege
`contents: read`. Workflow inputs reach shell steps only through step `env`
(never `${{ }}` interpolation), a scheduled run ignores any dispatch input, and
it publishes only from `main`, so a manual run on another ref cannot publish
unreviewed code. The private key is imported only when the run will publish; it
stays in `$RUNNER_TEMP` with mode `600` until the final always-run cleanup and is
used with `APT_SIGNING_KEY_ID` set to the full trusted fingerprint. Configure
these protected secrets:

- `APT_S3_BUCKET`, `APT_S3_ENDPOINT`, `APT_S3_REGION`,
  `APT_S3_ACCESS_KEY_ID`, `APT_S3_SECRET_ACCESS_KEY`
- `APT_RELEASE_SIGNING_KEY` (armored private key, imported only when a run
  publishes)
- `APT_SIGNING_FINGERPRINT` (full 40-hex primary fingerprint, enforced at
  verify and publish — not an echo)

The upstream source publishes only when upstream moved: a no-op builds, signs,
and uploads nothing and still succeeds. A manual manifest publish is refused when
the committed manifest is older than the published baseline, so publishing cannot
downgrade clients. No automatic key rotation occurs; `allow_key_rotation` is
accepted only for an explicit manual manifest publish.

Every published release binds the manifest it was built from into its signed
`Release`, and the updater refuses a release whose binding is missing or does
not match. Run one `publish.yml` manifest publish from `main` if the published
baseline needs to be rebuilt.

`ci.yml` runs the package build, verification, and native amd64/arm64 smoke on
every push, and pins `UV_DEFAULT_INDEX` to public PyPI so the committed lock
stays portable. It needs no cloud secrets: the CI signing key is ephemeral and
the Wrangler step is a dry run. The build and test jobs install `reprepro`,
which generates the `Packages` indices, so a missing tool fails the job
instead of silently producing a different index. Package publication and
Worker deploys have separate owners, so the GitHub environment holds only the
R2 and GPG material.

## 5. Client bootstrap

The repository is in public test mode, so a client needs no credentials: fetch
the armored key and add the source. See the installation guide served at `/` for
the exact three steps, reproduced here for operators.

- Install the armored public key with `sudo curl --fail --show-error --output
  /etc/apt/keyrings/apt-at-home.asc <host>/key.asc`, then `sudo chmod 0644` it.
  Debian 13's `apt-secure` accepts the `.asc` file directly; never use
  `apt-key`, `trusted=yes`, or `--dearmor`.
- Copy `client/personal.sources.example` to
  `/etc/apt/sources.list.d/apt-at-home.sources`, replacing the hostname. The
  `Signed-By` path must name the installed key file exactly:
  `/etc/apt/keyrings/apt-at-home.asc`.
- Verify the signing key by its full trusted primary fingerprint, comparing it
  against the fingerprint received through a separate channel — never a
  fingerprint printed by the gateway: `gpg --show-keys /etc/apt/keyrings/apt-at-home.asc`.
- If the credential gate is re-enabled, copy `client/auth.conf.example` to
  `/etc/apt/auth.conf.d/apt-at-home.conf`, replacing the device name and token,
  then `chmod 600` the file. Obtain the per-device username and token from the
  administrator out of band; tokens stay out of URLs, shell history, and logs.
- Repository verification by fingerprint:
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
- Neovim is the full upstream release tree, installed under `/usr` from one
  archive root. It declares `Conflicts` and `Replaces` on `neovim-runtime`, so an
  APT upgrade removes the distribution runtime through the normal solver. Its
  headless check exercises the packaged runtime and the bundled Lua tree-sitter
  parser.

## 6. WAF, limits, and alerts (limitations)

- The repository is in public test mode, so every anonymous request to an
  artifact path reaches R2 and counts against the Worker and storage request
  quotas. Treat the hostname as a public resource and watch usage; re-enable the
  credential gate (`AUTH_REQUIRED: "true"`) to stop anonymous traffic.
- A Worker-side WAF, rate limit, or alert cannot guarantee blocking of
  malicious invocations on the free tier; treat them as defense in depth.
- Keep tokens per-device and rotatable, watch Worker request logs for `401`
  bursts while the gate is enabled, and rotate any token that may have leaked.
- There is deliberately no `Valid-Until` on `Release`; clients do not expire
  metadata. This is a personal-repo freshness tradeoff documented in the
  manifest tooling.

## 7. Token rotation

1. Generate a new token for the device and add its digest to `CREDENTIALS`.
2. Update that device's `auth.conf` entry (`chmod 600`).
3. Confirm the device fetches through the Worker, then remove the old digest.
4. Never reuse a token across devices and never log tokens.
