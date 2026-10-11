# apt-at-home

Private per-device authenticated APT source for Debian 13. English | [中文](README.zh-CN.md)

## Features

- Private authenticated source with per-device tokens over an authenticated gateway.
- Debian 13 packages for amd64 and arm64: bottom, git-delta, neovim, rustfs-cli,
  sccache, starship, uv, zellij, and zoxide.
- Signed repository published via GitHub Actions to private object storage.
- Every six hours, `publish.yml` publishes a new release when a newer stable
  upstream release of any of those packages appears; when nothing changed it
  does nothing.

## Usage

1. Install the repository key, add the APT source with `Signed-By`, and set per-device credentials in `auth.conf`.
2. Then install packages:

```sh
sudo apt update
sudo apt install bottom git-delta neovim rustfs-cli sccache starship uv zellij zoxide
```

`bottom` provides `btm`, `git-delta` provides `delta`, `rustfs-cli` provides
`rc`, and `uv` provides both `uv` and `uvx`. `zoxide` shell integration is
opt-in; for Bash, add `eval "$(zoxide init bash)"` to your shell configuration if
wanted. Package installation does not change shell configuration.

`neovim` is the full upstream release tree (runtime, parsers, and man pages). It
replaces the distribution `neovim-runtime` package, so APT removes that package
during the upgrade.

See [docs/setup.md](docs/setup.md) for gateway and storage setup, [docs/publishing.md](docs/publishing.md) for the release flow, and [client/](client/) for example source and auth config.
