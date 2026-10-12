# Bun candidate

A locally built, unsigned `.deb` for [Bun](https://bun.sh) 1.4.3, packaged from
the official upstream release binaries.

This is a **candidate**: it is not in the production catalog, it is not signed,
and it is not published to the APT repository. Nothing about this page implies
that Bun is installable from APT.

## Why a candidate

Packaging upstream binaries is the part of this repository that is easy to get
subtly wrong: the download has to be pinned, the archive has to be inspected
before anything is written, the licences have to travel with the binary, and the
result has to be reproducible. Doing all of that for a tenth package means
editing the shared production pipeline.

The candidate path keeps Bun out of that pipeline entirely. Bun is absent from
`scripts/catalog.py`, so the updater never discovers it and the publisher can
never index it. The candidate builds through the same download, staging and
`dpkg-deb` code as every other package — only the descriptor and the entrypoint
are separate.

## Build it

```sh
uv run --frozen python -m scripts.bun_candidate --output dist/bun-candidate
```

Both architectures are built by default; pass `--arch amd64` or `--arch arm64`
for one. The pinned upstream assets are downloaded once into
`~/.cache/opencode/apt-bun-candidate/` and re-verified on every later build.
Rebuilding produces byte-identical packages.

## Check it

```sh
uv run --frozen python -m scripts.bun_candidate_smoke --pool dist/bun-candidate --arch amd64
```

The smoke checker reads the ELF header before it does anything else, so a
mislabelled asset is refused rather than executed. On a matching machine it then
runs three bounded checks — `bun --version`, `bunx --version`, and one in-memory
JavaScript evaluation — inside a private temporary `HOME`, with a minimal
environment and a 30 second limit. `--static-only` disables execution outright
and is never overridden by the host architecture. On a non-matching machine the
checks stay static and say so.

The probe environment is narrow, not sandboxed. Its proxy variables point at a
closed port, which deters a proxy-aware client from reaching a real proxy; it
does not disable networking, and a client that ignores the proxy variables is
not stopped by this tool.

To check an installed copy instead:

```sh
python3 -m scripts.bun_candidate_smoke --installed --arch amd64
```

## What is in the package

| Path | Contents |
|---|---|
| `/usr/bin/bun` | the official upstream binary, hash-verified |
| `/usr/bin/bunx` | a relative symlink to `bun`, owned by this package |
| `/usr/share/doc/bun/LICENSE.upstream.md` | upstream's `LICENSE.md` at tag `bun-v1.4.3`, byte for byte |
| `/usr/share/doc/bun/LGPL-2.0.txt` | GNU LGPL 2.0 text |
| `/usr/share/doc/bun/LGPL-2.1.txt` | GNU LGPL 2.1 text |
| `/usr/share/doc/bun/README.source` | where the corresponding source lives |
| `/usr/share/doc/bun/copyright` | provenance plus the licence texts |

The package depends on `libc6 (>= 2.17)` and nothing else; both release binaries
link only glibc.

## Licensing

Bun itself is MIT, but it statically links JavaScriptCore and WebKit, which are
LGPL-2. The Debian `License:` field is therefore the custom label `Bun-upstream`
rather than an MIT-only claim, and both GNU LGPL texts ship in the package
alongside the upstream notice.

## Source

The descriptor pins the exact upstream provenance. See
`packaging/bun/candidate.json` for the machine-readable form and
`packaging/bun/README.source` for the installed copy.

| Item | Value |
|---|---|
| Release tag | `bun-v1.4.3` |
| Bun source commit | `c6da4a4d3010e5553438c60f6bd76d981976867c` |
| WebKit / JavaScriptCore commit | `0c06faadf65bf8e8c8ad3a5a8aca83e1e9ed653f` |
| TinyCC commit | `05f0fafaa3be31e31d7b4b5c17dc60f62c991171` |

Upstream's own build steps live in the source tree at that exact tag: the
`build:local` script performs the build, and `sync-webkit-source` checks out the
WebKit commit pinned in `scripts/build/deps/webkit.ts` into `vendor/WebKit`
rather than downloading a prebuilt WebKit. See the tree itself at
<https://github.com/oven-sh/bun/tree/bun-v1.4.3>.

## Promotion

Publishing this candidate would be a separate decision with separate
requirements: signing, a catalog entry, and whatever the corresponding-source
obligation turns out to require for a binary-only repackage. None of that is
implied by this candidate, and none of it has been decided here.