# apt-at-home

Private per-device authenticated APT source for Debian 13. English | [中文](README.zh-CN.md)

## Features

- Private authenticated source with per-device tokens over an authenticated gateway.
- Debian 13 packages for amd64 and arm64: bottom, bun, git-delta, kandev, neovim,
  rustfs-cli, sccache, starship, uv, zellij, and zoxide, plus cargo-tauri for
  amd64 only from its official release asset.
- Signed repository published via GitHub Actions to private object storage.
- Every six hours, `publish.yml` publishes a new release when a newer stable
  upstream release of any of those packages appears; when nothing changed it
  does nothing. `cargo-tauri` follows the highest stable `tauri-cli-vN.N.N` tag
  and its exact release's official amd64 asset.

## Usage

1. Install the repository key, add the APT source with `Signed-By`, and set per-device credentials in `auth.conf`.
2. Then install packages:

```sh
sudo apt update
sudo apt install bottom bun git-delta kandev neovim rustfs-cli sccache starship uv zellij zoxide
```

On amd64 only:

```sh
sudo apt install cargo-tauri
```

`bottom` provides `btm`, `git-delta` provides `delta`, `rustfs-cli` provides
`rc`, and `uv` provides both `uv` and `uvx`. `bun` provides `bun`; upstream's
`bunx` command stays available as `bun x`. `cargo-tauri` (amd64 only) provides
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
