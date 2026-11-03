# apt-at-home

Private per-device authenticated APT source for Debian 13. English | [中文](README.zh-CN.md)

## Features

- Private authenticated source with per-device tokens over an authenticated gateway.
- Debian 13 packages for amd64 and arm64: bottom, bun, cargo-tauri, chezmoi,
  fd-find, forgejo-cli, fzf, git-delta, glab, kandev, neovim, opencode, pi,
  rustfs-cli, sccache, starship, tree-sitter-cli, uv, zellij, and zoxide, each
  from its reviewed upstream release.
- `forgejo-cli` comes from the `cloudiful/forgejo-cli` personal-use fork, not
  from the upstream official distribution; it adds `fj pr attach`. Its archives
  are pinned by the sha256 digest the fork's GitHub release reports for each of
  them.
- `glab` comes from the official GitLab release archives. GitLab publishes an
  unsigned `checksums.txt` instead of a cryptographic signature, so each
  archive is pinned by the SHA256 that file names; that digest trusts GitLab's
  TLS transport and repository integrity.
- Signed repository published via GitHub Actions to private object storage.
- Every six hours, `publish.yml` publishes a new release when a newer stable
  upstream release of any of those packages appears; when nothing changed it
  does nothing. `cargo-tauri` follows the highest stable `tauri-cli-vN.N.N` tag
  and that exact release's official Linux assets.

## Usage

1. Install the repository key, add the APT source with `Signed-By`, and set per-device credentials in `auth.conf`.
2. Then install packages:

```sh
sudo apt update
sudo apt install bottom bun cargo-tauri chezmoi fd-find forgejo-cli fzf git-delta glab kandev neovim opencode pi rustfs-cli sccache starship tree-sitter-cli uv zellij zoxide
```

`bottom` provides `btm`, `fd-find` provides `fdfind`, `forgejo-cli` provides
`fj`, `git-delta` provides `delta`, `glab` provides `glab`, `rustfs-cli`
provides `rc`, `tree-sitter-cli` provides `tree-sitter`, and `uv` provides both
`uv` and `uvx`. `bun` provides `bun`; upstream's
`bunx` command stays available as `bun x`. `cargo-tauri` provides `cargo-tauri`
directly and `cargo tauri` as a Cargo external subcommand (it requires `cargo`
on `PATH`, which the package declares as a dependency). `zoxide` shell integration is
opt-in; for Bash, add `eval "$(zoxide init bash)"` to your shell configuration if
wanted. Package installation does not change shell configuration.

`tree-sitter-cli` requires glibc 2.39 or newer. `chezmoi` never edits shell
configuration; run it explicitly to apply dotfiles.

`opencode` is OpenCode V2, installed as the standalone `opencode` command: it
needs neither Node nor Bun. The amd64 build requires AVX2, and both builds
need glibc 2.17 or newer. Versions follow the stable 2.x line of the official
npm distribution, whose archives are verified against the registry's published
integrity digest before their SHA256 is pinned. Each `.deb` is large because
the packaged binary is about 240 MB uncompressed.

`kandev` installs the upstream standard server bundle. Run `kandev` to start it;
package installation does not start the server.

`pi` installs the complete upstream standalone bundle under `/usr/lib/pi`:
Pi's executable plus its themes, assets, export templates, documentation,
examples, the Photon image-processing WASM module, and the native X11
clipboard helper. `/usr/bin/pi` launches it from there and forces Pi's
self-update command to the packaged `/usr/lib/pi/apt-update` helper: that
helper only prints a `sudo apt upgrade pi` or
`sudo apt install --only-upgrade pi` instruction to stderr and exits nonzero,
so update Pi through APT. Pi's extension and model update commands remain
available and pass through the launcher unfiltered. Root or a manual direct
run of `/usr/lib/pi/pi` can still change files and bypass the launcher, and Pi
has no built-in permission sandbox.

`neovim` is the full upstream release tree (runtime, parsers, and man pages). It
replaces the distribution `neovim-runtime` package, so APT removes that package
during the upgrade.

See [docs/setup.md](docs/setup.md) for gateway and storage setup, [docs/publishing.md](docs/publishing.md) for the release flow, and [client/](client/) for example source and auth config.
