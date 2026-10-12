# apt-at-home

Private per-device authenticated APT source for Debian 13. English | [中文](README.zh-CN.md)

## Features

- Private authenticated source with per-device tokens over an authenticated gateway.
- Debian 13 packages for amd64 and arm64: bottom, cargo-tauri, git-delta, kandev, neovim,
  rustfs-cli, sccache, starship, uv, zellij, and zoxide.
- Signed repository published via GitHub Actions to private object storage.
- Every six hours, `publish.yml` publishes a new release when a newer stable
  upstream release of any of those packages appears; when nothing changed it
  does nothing. `cargo-tauri` follows official `tauri-cli-vN.N.N` tags
  automatically: amd64 repackages the official asset, arm64 is compiled from
  pinned source on native ARM.

## Usage

1. Install the repository key, add the APT source with `Signed-By`, and set per-device credentials in `auth.conf`.
2. Then install packages:

```sh
sudo apt update
sudo apt install bottom cargo-tauri git-delta kandev neovim rustfs-cli sccache starship uv zellij zoxide
```

`bottom` provides `btm`, `git-delta` provides `delta`, `rustfs-cli` provides
`rc`, and `uv` provides both `uv` and `uvx`. `cargo-tauri` provides
`cargo-tauri` directly and `cargo tauri` as a Cargo external subcommand (it
requires `cargo` on `PATH`, which the package declares as a dependency). `zoxide` shell integration is
opt-in; for Bash, add `eval "$(zoxide init bash)"` to your shell configuration if
wanted. Package installation does not change shell configuration.

`kandev` installs the upstream standard server bundle. Run `kandev` to start it;
package installation does not start the server.

`neovim` is the full upstream release tree (runtime, parsers, and man pages). It
replaces the distribution `neovim-runtime` package, so APT removes that package
during the upgrade.

See [docs/setup.md](docs/setup.md) for gateway and storage setup, [docs/publishing.md](docs/publishing.md) for the release flow, and [client/](client/) for example source and auth config.

A locally built, unsigned Bun candidate package is documented in [docs/bun-candidate.md](docs/bun-candidate.md). It is not published and not installable from this APT repository.

A locally built, unsigned Tauri CLI candidate package — the official amd64 release asset and an arm64 package compiled from pinned source — is documented in [docs/tauri-cli-candidate.md](docs/tauri-cli-candidate.md). Cargo-tauri is integrated as production package eleven in the catalog; live-repository publication remains a parent-verified delivery gate.
