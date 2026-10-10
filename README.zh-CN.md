# apt-at-home

面向 Debian 13 的按设备鉴权私有 APT 源。[English](README.md) | 中文

## 特性

- 私有鉴权源：经鉴权网关按设备令牌访问。
- 面向 Debian 13 的 amd64 与 arm64 软件包：zellij 与 starship。
- 经 GitHub Actions 签名并发布到私有对象存储的仓库。
- `update.yml` 每六小时检查一次：上游出现更新的稳定版 zellij 或 starship 时发布新版本，否则不做任何操作。

## 用法

1. 安装仓库密钥，使用 `Signed-By` 添加 APT 源，并在 `auth.conf` 中配置按设备凭据。
2. 然后安装软件包：

```sh
sudo apt update
sudo apt install zellij starship
```

网关与存储配置见 [docs/setup.md](docs/setup.md)，发布流程见 [docs/publishing.md](docs/publishing.md)，示例源与鉴权配置见 [client/](client/)。
