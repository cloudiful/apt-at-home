# apt-at-home

私有 Debian 13 APT 软件源，含鉴权 Cloudflare Worker 网关与 R2 存储。 [English](README.md) | 中文

## 项目内容

- 鉴权 Worker 网关（`worker/`）：按设备 Basic Auth 令牌，仅 `GET`/`HEAD`，
  先鉴权后缓存，`Range`/`If-*` 回源求值，`current.json` 发布指针与保留的
  by-hash 索引。账户 `12412d5b061683494ea3e5c217d6a961`，桶 `apt`。
- 本地打包流水线（`packaging/`、`scripts/`）：固定的 zellij 0.45.1 与
  starship 1.26.0 musl 二进制（amd64/arm64），四个可复现 `.deb`（`-1`
  修订，`root/root`），签名后的 APT 元数据，指纹锁定的发布
  （`APT_SIGNING_FINGERPRINT`）。
- 手动 GitHub Actions 发布，以及基于基础配置 `--domains`/`--secrets-file`
  的独立手动 Worker 部署（依赖 `APT_WORKER_DOMAIN` 与 `CREDENTIALS` 门禁）。
- 客户端使用 `Signed-By` 与 `auth.conf`（`chmod 600`）；不用 `apt-key` 或
  `trusted=yes`。

## 快速开始（仅本地）

```sh
bun install
bun run typecheck
bun run test
uv sync
uv run python -m unittest discover -s tests
uv run python -m scripts.repo --help
```

构建并验证签名树（需要本地签名密钥文件）：

```sh
APT_SIGNING_KEY_FILE=/path/to/private.asc APT_SIGNING_KEY_ID=<fingerprint> \
  uv run python -m scripts.repo build --output dist/repo
APT_SIGNING_FINGERPRINT=<40-hex primary> uv run python -m scripts.repo verify --output dist/repo
```

运维的 bucket、域名、密钥与令牌轮换见 [docs/setup.md](docs/setup.md)，
手动发布流程见 [docs/publishing.md](docs/publishing.md)。客户端占位配置在
`client/`。

## 仓库布局

- `pool/*.deb`：不可变软件包文件。
- `releases/<id>/dists/stable/...`：签名发布（`Release`、`InRelease`、
  `Release.gpg`，各架构 `Packages`、`Packages.gz`）。
- `indices/<arch>/SHA256/<digest>`：共享 by-hash 内容，指针切换后保留。
- `current.json`：选择完整发布。`key.asc`：仓库公钥。

## 安全说明

- 本 checkout 无 Web 界面、上传 API，也不会执行部署。
- 从不覆盖不可变对象，从不 `--delete` 同步，远端校验通过后才移动指针；
  生产发布（含轮换）必须校验完整可信指纹。
- `Release` 无 `Valid-Until`（个人源的新鲜度取舍）。
- WAF 或限流无法保证挡住恶意 Worker 调用。
