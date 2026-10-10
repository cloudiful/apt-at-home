# apt-at-home

Private per-device authenticated APT source for Debian 13. English | [中文](README.zh-CN.md)

## Features

- Private authenticated source with per-device tokens over an authenticated gateway.
- Debian 13 packages for amd64 and arm64: git-delta, rustfs-cli, sccache,
  starship, and zellij.
- Signed repository published via GitHub Actions to private object storage.
- Every six hours, `publish.yml` publishes a new release when a newer stable
  upstream release of any of those packages appears; when nothing changed it
  does nothing.

## Usage

1. Install the repository key, add the APT source with `Signed-By`, and set per-device credentials in `auth.conf`.
2. Then install packages:

```sh
sudo apt update
sudo apt install git-delta rustfs-cli sccache starship zellij
```

The Debian package names and the commands they install differ for two of them:
`git-delta` provides `delta`, and `rustfs-cli` provides `rc`.

See [docs/setup.md](docs/setup.md) for gateway and storage setup, [docs/publishing.md](docs/publishing.md) for the release flow, and [client/](client/) for example source and auth config.
