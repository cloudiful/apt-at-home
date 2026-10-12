# apt-at-home

面向 Debian 13 的按设备鉴权私有 APT 源。[English](README.md) | 中文

## 特性

- 私有鉴权源：经鉴权网关按设备令牌访问。
- 面向 Debian 13 的 amd64 与 arm64 软件包：bottom、git-delta、kandev、neovim、rustfs-cli、sccache、starship、uv、zellij 与 zoxide。
- 经 GitHub Actions 签名并发布到私有对象存储的仓库。
- `publish.yml` 每六小时检查一次：其中任一软件包出现更新的上游稳定版时发布新版本，否则不做任何操作。

## 用法

1. 安装仓库密钥，使用 `Signed-By` 添加 APT 源，并在 `auth.conf` 中配置按设备凭据。
2. 然后安装软件包：

```sh
sudo apt update
sudo apt install bottom git-delta kandev neovim rustfs-cli sccache starship uv zellij zoxide
```

`bottom` 提供 `btm`，`git-delta` 提供 `delta`，`rustfs-cli` 提供 `rc`，`uv` 同时提供 `uv` 与 `uvx`。`zoxide` 的 shell 集成是可选的；如需用于 Bash，可自行将 `eval "$(zoxide init bash)"` 添加到 shell 配置中。安装软件包不会修改 shell 配置。

`kandev` 安装上游标准服务器套件；运行 `kandev` 才会显式启动服务器，
安装软件包不会启动服务器。

`neovim` 是完整的上游发布目录树（运行时、解析器与手册页）。它会替代发行版的 `neovim-runtime` 包，因此升级时 APT 会移除该包。

网关与存储配置见 [docs/setup.md](docs/setup.md)，发布流程见 [docs/publishing.md](docs/publishing.md)，示例源与鉴权配置见 [client/](client/)。

本地构建的、未签名的 Bun 候选包说明见 [docs/bun-candidate.md](docs/bun-candidate.md)。它尚未发布，也无法从本 APT 仓库安装。
