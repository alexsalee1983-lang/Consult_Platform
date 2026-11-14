# 对象存储镜像

MinIO 社区版，从源码编译（社区版官方镜像已于 2026 年下架，决策记录 2026-11-08）。`docker compose` 会自动构建。

| 组件 | 版本 |
|---|---|
| MinIO | RELEASE.2025-10-15T17-29-55Z（commit 9e49d5e） |
| 编译环境 | golang:1.26.11-bookworm |
| 运行环境 | debian:bookworm-20261027-slim，非 root 用户运行 |

- 源码包固定版本并校验 sha256，Go 依赖由 `go.sum` 校验。升级时同时改 `Dockerfile` 里的版本号、commit 和 sha256，并在 PR 里说明。
- 第一次构建要下载约 25MB 源码和 Go 依赖，再编译，大约需要 3～8 分钟。
- 构建机连不上外网时：在 `.env` 里设 `MINIO_SRC_URL`（源码包内部地址）和 `GOPROXY`（公司内部 Go 代理）。
- 只在内部网络开放 9000 端口（S3 接口），不对外映射，不启用管理控制台。
- **服务端加密**：用 `MINIO_KMS_SECRET_KEY` 配一把静态主密钥，所有对象落盘自动加密（`MINIO_KMS_AUTO_ENCRYPTION=on`），平台建桶时再设一次桶默认加密。**主密钥丢失则数据无法解密**，要和数据卷分开备份。
- 平台代码只经 S3 协议访问（`backend/app/services/storage.py`），以后可换成 SeaweedFS 等其他 S3 兼容存储，迁移时用工具复制对象即可。
- MinIO 以 AGPL-3.0 许可发布，这里只在内部原样使用、不修改源码。
