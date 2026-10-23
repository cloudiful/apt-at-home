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
- The exact root path `/` is the one public exception: `GET`/`HEAD /` serves the
  public landing page before the credential check, so a misconfigured runtime
  still explains how to bootstrap a client. The page follows the browser's
  `Accept-Language` (English by default), and `?lang=en` or `?lang=zh`
  overrides it. It reads only the published release metadata — the release
  pointer and the manifest of the release it names — and never the credential
  secret, so it exposes nothing beyond the published package list. Every other
  path stays behind the credential gate, and a `503` there means the runtime
  credentials secret is missing or unusable — fix it under Worker → Settings →
  Variables and Secrets. The guide only installs anything after the first
  successful release publishes the repository.
- The landing page lists the packages of the current published release, read
  from the manifest that release carries, so a publish changes the page without
  a Worker redeploy. The metadata is cached for one minute and the page for
  another minute, so a new release becomes visible within roughly two minutes.
  When the metadata cannot be read — no pointer, no manifest, a corrupt
  document, or a storage error — the page still renders and states that the
  package list is unavailable; it never presents an older list as current.
- The page is a React application: the Worker renders it on the server and the
  browser hydrates it, so the package table and the install commands always
  come from the same published catalog. Without JavaScript the document still
  carries the table and the commands. The header holds the language menu, the
  theme toggle and the repository link; the theme follows the saved choice or
  the system preference, and a small script in the document head applies it
  before the first paint.
- The page's own code and styles are public assets: the Worker serves them
  from `/assets/` and nothing else is public there. Every repository path —
  `/dists`, `/pool`, `/key.asc` — stays behind the credential gate, and the
  assets never reach the bucket.

## 3. Cloudflare Worker Builds (deploy owner)

Cloudflare builds and deploys the gateway straight from this GitHub
repository, so no GitHub workflow deploys the Worker.

- In the Cloudflare dashboard, create the Worker and connect its Git
  integration to this repository. Use the repository root as the build root.
- Set the build variable `SKIP_DEPENDENCY_INSTALL=1`. This supported switch
  stops the automatic Python/uv dependency install, so the build stays
  JavaScript-only and never resolves a Python index.
- Build command: `npm ci && npm run typecheck && npm test`. The typecheck and
  the test run the browser build first, so the generated asset manifest the
  Worker imports always exists. Node 20 or newer is required.
- Deploy command:
  `npx wrangler deploy --config worker/wrangler.jsonc --domains apt.example.com`
  (replace the example hostname with the operator's own domain). The Wrangler
  build step builds the browser assets before the Worker is bundled.
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
- Checksum provenance: archive SHA256 values in `packaging/manifest.json`
  were verified by direct download against the GitHub API digests. Zellij
  upstream `.sha256sum` files cover the inner binary
  (`target/<triple>/release/zellij`), not the tarball; starship
  `.tar.gz.sha256` files cover the tarball. OpenCode publishes through the npm
  registry, which carries a sha512 subresource-integrity digest for every
  package version: each platform archive is verified in full against that
  digest before its SHA256 is computed and pinned, and registry signature blobs
  are not treated as verified authenticity. GitLab publishes an unsigned
  `checksums.txt` beside each release, so the `glab` archive digests are read
  from that file and trust GitLab's TLS transport and repository integrity
  rather than a cryptographic signature. See `checksum_provenance` in the
  manifest.
- `forgejo-cli` comes from Codeberg, which publishes no release digest: its
  archives are pinned by a SHA256 this repository computes from the official
  download, so the first pinned digest trusts that first download and every
  later discovery recomputes it. One discovery run downloads about 18 MiB of
  Codeberg archives, and an unreachable archive, a renamed asset, or changed
  license bytes stops the update instead of skipping the package.
- `glab` comes from GitLab's official release tarballs. GitLab publishes an
  unsigned `checksums.txt` beside each release instead of a per-asset digest or
  signature, so the selected archive digests are read from that file; an
  unreachable or renamed archive, a foreign download link, a malformed or
  missing selected checksum, a non-canonical release entry, or changed license
  bytes stops the update instead of skipping the package.
- `opencode` comes from the official npm registry, whose archives are read in
  full to compute their SHA256 after the integrity check. One discovery run
  downloads about 220 MiB of OpenCode archives; a missing, malformed, or
  mismatching digest, a deprecated release, or a platform package the wrapper
  does not pin at the same exact version stops the update instead of skipping
  the package.
- Neovim is the full upstream release tree, installed under `/usr` from one
  archive root. It declares `Conflicts` and `Replaces` on `neovim-runtime`, so an
  APT upgrade removes the distribution runtime through the normal solver. Its
  headless check exercises the packaged runtime and the bundled Lua tree-sitter
  parser.
- The command-line packages are `bottom` (`btm`), `bun` (`bun`), `cargo-tauri`
  (`cargo-tauri` and `cargo tauri`, requires `cargo`), `chezmoi`, `fd-find`
  (`fdfind`), `forgejo-cli` (`fj`), `fzf`, `git-delta` (`delta`), `glab`
  (static, no runtime dependencies), `neovim`
  (`nvim`), `opencode` (OpenCode V2, standalone: no Node or Bun; amd64 needs
  AVX2 and both architectures need glibc 2.17), `rustfs-cli` (`rc`), `sccache`,
  `starship`, `tree-sitter-cli` (`tree-sitter`, requires glibc 2.39), `uv`
  (`uv` and `uvx`), `zellij`, and `zoxide` for both architectures. The arm64
  `cargo-tauri` build needs glibc 2.18 and the amd64 build keeps its glibc 2.34
  and liblzma requirements. Zoxide shell initialization is opt-in; users may
  add `eval "$(zoxide init bash)"` to their own Bash configuration. Package
  installation never edits shell configuration.
- `fd-find` installs upstream's `fd` binary as `fdfind`; `/usr/bin/fd` is left
  to the distribution's unrelated `fdclone` package.
- `bun` ships upstream's official release ZIP, whose binary is the only member
  the package installs; upstream's `bunx` command stays available as `bun x`.

## 6. WAF, limits, and alerts (limitations)

- The repository is in public test mode, so every anonymous request to an
  artifact path reaches R2 and counts against the Worker and storage request
  quotas. Treat the hostname as a public resource and watch usage; re-enable the
  credential gate (`AUTH_REQUIRED: "true"`) to stop anonymous traffic.
- The gateway applies three per-IP request budgets to artifact `GET`/`HEAD`
  requests through first-class Wrangler bindings in `worker/wrangler.jsonc`:

  | Budget | Binding | Attempts per minute | Covers |
  | --- | --- | --- | --- |
  | Metadata | `INDEX_LIMITER` | 120 | `/key.asc`, `dists/` metadata and by-hash indexes |
  | Downloads | `DOWNLOAD_LIMITER` | 30 | every `/pool/<flat filename>.deb` request |
  | Per package | `PACKAGE_LIMITER` | 6 | one canonical `/pool/<flat filename>.deb` |

  The package budgets are counted per canonical file name, not per package
  name: a version or architecture is a separate file bucket, while an encoded
  alias or a query string still counts against the same file. Pool requests are
  checked against the download budget first and then the per-file budget; a
  denial in either case answers `429` with `Retry-After: 60`, `no-store`, and no
  authentication challenge. That delay is advisory, not the counter's exact
  reset.
- The public landing page states these same budgets in its fair-use section: the
  numbers come from one exported constant that a test binds to this file, so the
  page cannot drift from the deployed limits. Edit the `simple` values above and
  the page follows.
- Only the Cloudflare-resolved `CF-Connecting-IP` header identifies a client.
  Cookies, `X-Forwarded-For`, and authorization values never select a bucket,
  and the address is neither persisted nor logged by the gateway. A request
  without that address — local or internal invocation — skips throttling rather
  than sharing one bucket.
- The budgets are best-effort abuse protection, not an authorization gate and
  not a hard quota. A missing or failing binding fails open: that budget is
  skipped, the request continues through the normal authentication and storage
  flow, and every other healthy budget still applies. Counters are kept per
  Cloudflare location, are eventually consistent, and use a one-minute window,
  so they do not measure daily usage, per-user totals, or devices behind NAT;
  IPv6 clients are counted per address. Limiting is checked inside the Worker
  invocation, so it cannot prevent that invocation from being billed.
- Change a budget by editing its `simple` value in `worker/wrangler.jsonc` and
  redeploying; the committed file is the source of truth. The namespace IDs are
  reserved for this project and must stay stable across deployments. Do not
  reuse them in another Worker in the same account; confirm that before a
  separately authorized production deploy.
- The public landing page reads the release pointer and the release manifest on
  a cold cache, so it reaches R2 as well; those reads are cached for one
  minute. A Worker-side WAF, rate limit, or alert cannot guarantee blocking of
  malicious invocations on the free tier; treat them as defense in depth.
- For a local check, run
  `bun x wrangler dev --config worker/wrangler.jsonc --local --port 8787 --persist-to .cache/ai/rate-limit/state`
  and request only known-missing files so no bulk download starts:
  `/pool/rate-limit-probe_1.0-1_amd64.deb` answers `404` and then `429` once its
  per-file budget is spent, and repeated `/key.asc` requests exercise the
  metadata budget. Local counters are approximate and reset with the local
  window or the persisted state directory.
- Keep tokens per-device and rotatable, watch Worker request logs for `401`
  bursts while the gate is enabled, and rotate any token that may have leaked.
- There is deliberately no `Valid-Until` on `Release`; clients do not expire
  metadata. This is a personal-repo freshness tradeoff documented in the
  manifest tooling.

## 7. Anonymous public counters

The landing page publishes two anonymous counters over the last 30 days: the
homepage visits and the package download requests. They are counts of requests
the gateway answered, not of people and not of installations.

- What is counted: a `GET` of `/` that answered `200` with the homepage, and a
  `GET` of a validated `/pool/<file>.deb` that answered `200` or `206`. Every
  other request is not counted — no `HEAD`, no browser asset, no metadata
  request, and no request that failed (`4xx`, `5xx`, `304`, `416`). One client
  request is one count even when the answer came from the cache; range
  requests, retries and bots each count as their own request. A count is
  therefore a request count and never a completed download.
- What is stored: one event per counted request carrying only the kind of the
  request (`visit` or `download`) and a count of one. No address, no path, no
  header, no referrer, no cookie, no device or account identifier, and no
  hashed address is written or derived. The only timestamp is the one the
  provider adds.
- What is read back: one fixed query over the bound dataset
  (`events.analyticsEngine.apt_at_home_events`), filtered to the two known
  kinds and the last 30 days. The query is a constant in the Worker; no
  request can influence the table, the column or the filter, and the analytics
  SQL binding already accounts for the sampling of the dataset, so the totals
  are not scaled a second time.
- The counters only exist from the moment the dataset is enabled and the
  Worker is deployed. There is no backfill: earlier traffic is not counted.
  The provider keeps the underlying events for 90 days.
- The numbers are approximate. The page shows them rounded, and the page
  itself says so. They lag: the successful answer is reused for five minutes,
  so a fresh request is normally reflected a few minutes after it happened,
  and the current homepage request is reflected on a later read.
- When the counters cannot be read — no binding, a failed query, a query that
  runs past its half-second budget, or an answer the Worker cannot read — the
  page shows no counters at all. A missing counter and a counter of zero are
  different facts and are never shown as the same thing; a read that succeeds
  with no rows really did count zero requests.
- The two bindings are committed in `worker/wrangler.jsonc`: the dataset
  binding `ANALYTICS` (dataset `apt_at_home_events`) and the query binding
  `ANALYTICS_SQL`. The dataset is created by the provider on the first write
  after the first deployment; nothing is provisioned from this checkout.
- Reading the counters back needs the account to have Analytics Engine and
  Analytics SQL available. That entitlement is a deployment prerequisite: it
  is not asserted here, and no live write, query or provisioning happens from
  this checkout. After a deployment, confirm that the dataset exists and that
  the query answers before relying on the published numbers.

## 8. Token rotation

1. Generate a new token for the device and add its digest to `CREDENTIALS`.
2. Update that device's `auth.conf` entry (`chmod 600`).
3. Confirm the device fetches through the Worker, then remove the old digest.
4. Never reuse a token across devices and never log tokens.
