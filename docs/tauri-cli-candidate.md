# Tauri CLI candidate

A locally built, unsigned `.deb` for the [Tauri CLI](https://tauri.app) 2.12.2,
installing `cargo-tauri`.

This is a **candidate**: it is not in the production catalog, it is not signed,
and it is not published to the APT repository. Nothing about this page implies
that the Tauri CLI is installable from APT.

The two architectures do not come from the same kind of input. Upstream publishes
a release asset for Linux amd64 and none for Linux arm64, so:

| Architecture | Binary | Provenance |
|---|---|---|
| `amd64` | `/usr/bin/cargo-tauri` | the official upstream release asset, downloaded and verified against its SHA-256 digest |
| `arm64` | `/usr/bin/cargo-tauri` | compiled from the pinned upstream commit by a native ARM64 CI job |

Only the amd64 package contains an official upstream binary. The arm64 package is
a source build and says so in its own installed record.

## Why a candidate

Packaging upstream binaries is the part of this repository that is easy to get
subtly wrong: the download has to be pinned, the archive has to be inspected
before anything is written, the licences have to travel with the binary, and the
result has to be reproducible. Doing that for a new package — and doing it twice,
from two different kinds of input — means touching the shared production pipeline.

The candidate path keeps the Tauri CLI out of that pipeline. It is absent from
`scripts/catalog.py`, so the updater never discovers it and the publisher can
never index it. The candidate builds through the same staging and `dpkg-deb` code
as every other package — only the descriptor, the entrypoint, and the two
provenance records are separate.

## Build the amd64 candidate

```sh
uv run --frozen python -m scripts.tauri_candidate --arch amd64 --output dist/tauri-candidate
```

`amd64` is the default: it is the only architecture with an official asset, and it
is the one that needs nothing but this repository. The pinned asset is downloaded
once into `~/.cache/opencode/apt-tauri-candidate/` and re-verified against its
digest on every later build. Rebuilding the same binary produces byte-identical
packages.

## Build the arm64 candidate

The arm64 package is compiled from source on a native ARM64 Linux machine; there
is nothing to download here. Its build produces two inputs, and both are
required:

```sh
# on the ARM64 build machine, from a checkout of the pinned upstream commit
cargo build --locked --manifest-path crates/tauri-cli/Cargo.toml \
  --target aarch64-unknown-linux-gnu --profile release-size-optimized --bin cargo-tauri

python3 -m scripts.tauri_candidate_provenance emit-source-build \
  --upstream <tauri checkout> --binary <target/aarch64-unknown-linux-gnu/release-size-optimized/cargo-tauri> \
  --rustc "$(rustc -Vv)" --cargo "$(cargo -V)" --output build-provenance.json
```

```sh
# anywhere with the two of them
uv run --frozen python -m scripts.tauri_candidate --arch arm64 \
  --output dist/tauri-candidate \
  --binary <cargo-tauri> --provenance build-provenance.json
```

The build refuses a binary whose digest, size, or architecture the record does
not describe, and refuses a record whose commit, lockfile, toolchain, command, or
architecture is not the reviewed one. Building `--arch arm64` without both inputs
is refused outright: the candidate never invents an upstream arm64 asset URL or a
digest for it.

## Check it

```sh
uv run --frozen python -m scripts.tauri_candidate_smoke --pool dist/tauri-candidate --arch amd64
```

The checker reads the ELF header and its dynamic section before it does anything
else, so a mislabelled binary is refused rather than executed. It reads two things
from that dynamic section: the `DT_NEEDED` library names, and the symbol versions
each library must provide. A library the reviewed mapping does not cover is a
refusal, and so is a library whose required versions exceed the floor the mapping
declares — a binary needing `GLIBC_2.40` cannot be satisfied by the declared
`libc6 (>= 2.34)`, and the candidate never widens a floor to paper over that. The
measured floors are reported so a reader can see them.

It then checks the installed licence texts byte for byte against the reviewed
upstream files, checks the source document and the architecture's provenance
record, and compares the control fields with the reviewed version, architecture,
and dependencies.

On a matching machine it then runs two bounded checks — `cargo-tauri --version`
and `cargo tauri --version`, the latter through a real Cargo with `PATH` pointing
at this package's own `usr/bin` — inside a private temporary `HOME`, with a
minimal environment and a 30 second limit. Nothing is initialised, built, or
bundled. `--static-only` disables execution outright and is never overridden by
the host architecture; `--require-cargo` and `--require-native` turn the two skips
into failures, which is what CI does. On a non-matching machine the checks stay
static and say so.

The probe environment is narrow, not sandboxed. Its proxy variables point at a
closed port, which deters a proxy-aware client from reaching a real proxy; it
does not disable networking, and a client that ignores the proxy variables is
not stopped by this tool.

To check an installed copy instead:

```sh
python3 -m scripts.tauri_candidate_smoke --installed --arch amd64 --require-native --require-cargo
```

## What is in the package

| Path | Contents |
|---|---|
| `/usr/bin/cargo-tauri` | the payload, verified for this architecture |
| `/usr/share/doc/cargo-tauri/LICENSE-MIT.txt` | upstream's `LICENSE-MIT` at tag `tauri-cli-v2.12.2`, byte for byte |
| `/usr/share/doc/cargo-tauri/LICENSE-APACHE-2.0.txt` | upstream's `LICENSE-APACHE-2.0` at that tag, byte for byte |
| `/usr/share/doc/cargo-tauri/README.source` | where the corresponding source lives and how it was built |
| `/usr/share/doc/cargo-tauri/official-asset.json` | amd64 only: the pinned asset and the measured binary and licence digests |
| `/usr/share/doc/cargo-tauri/build-provenance.json` | arm64 only: the measured source build, toolchain, lockfile, and artifact digest |
| `/usr/share/doc/cargo-tauri/copyright` | provenance plus the licence texts |

Both records are unsigned descriptions of one build, not cryptographic proofs of
origin. They exist so a reader can tell where a binary came from without trusting
this repository's claims.

## Dependencies

`Depends` is derived from the closed runtime mapping rather than written by hand,
and the mapping is pinned in the descriptor *and* in the code that reads it, so a
floor cannot be widened by editing one file. Reading a binary's library names is
not enough to justify a floor: glibc keeps one SONAME across many versions, so
what a binary actually requires is the highest symbol version it names. Every
build therefore reads the symbol-version requirements out of the ELF and refuses
to package a binary whose requirements exceed the declared floor, rather than
claiming a dependency the build has not earned.

Not every library offers a namespace to measure that way. Debian's `libbz2`
exports no symbol versions at all, so its entry records the library as
unversioned and its floor comes from the reviewed package version instead. A
binary that claimed symbol versions from it would be describing a different
library, and the build refuses it rather than accepting it on an unchecked
rule.

```
Depends: cargo, libbz2-1.0 (>= 1.0.8-6), libc6 (>= 2.39), libgcc-s1 (>= 14.2.0), liblzma5 (>= 5.8.1)
```

Both architectures declare that same list, because both are built from the one
mapping. The arm64 build requires `GLIBC_2.39` from `libc.so.6`, `GLIBC_2.29`
from `libm.so.6`, `GCC_4.2.0`, and `XZ_5.0`, and it links libbz2, which requires
no version at all. The amd64 binary at this release needs less of the same
libraries — `GLIBC_2.34` from glibc, the same `GCC_4.2.0` and `XZ_5.0` — and does
not link libbz2. The `libc6` floor is the higher of the two measured
requirements, which Debian 13 satisfies; it is conservative for amd64, and every
smoke run prints the measured floor beside the declared one so the difference is
reported rather than implied. Deriving the list per architecture is production
packaging work, not candidate work.

`cargo` is a real dependency rather than a recommendation: `cargo tauri` is a
Cargo external subcommand, so the package is only usable the way users invoke it
when Cargo is present. The ARM build is held to the same mapping — if the
compiled binary needs a newer library than this declares, the build fails and the
mapping is reviewed before it changes.

## Licensing

Upstream is dual licensed, `MIT OR Apache-2.0`, and both texts ship in the
package and are quoted in the `copyright` file.

## Source

The descriptor pins the exact upstream provenance. See
`packaging/tauri-cli/candidate.json` for the machine-readable form and
`packaging/tauri-cli/README.source` for the installed copy.

| Item | Value |
|---|---|
| Release tag | `tauri-cli-v2.12.2` |
| Source commit | `5b23c82ea5ba6b9aec420dd71f60493edc486aa9` |
| `Cargo.lock` git blob | `6b052c58398b8d6ec246d77f65a748448cd4c859` |
| Rust toolchain | `1.99.0` |
| Target triple | `aarch64-unknown-linux-gnu` |
| Cargo profile | `release-size-optimized` |

## Promotion

Publishing this candidate would be a separate decision with separate
requirements: signing, a catalog entry, a runtime dependency review for whatever
the arm64 build actually links, and whatever the corresponding-source obligation
turns out to require for the binary-only amd64 package. None of that is implied
by this candidate, and none of it has been decided here.
