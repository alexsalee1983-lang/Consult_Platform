# 对象存储镜像：MinIO 社区版，从源码编译（社区版官方镜像已下架，决策记录 2026-11-08）。
#
# 平台代码只经 S3 协议访问对象存储（backend/app/services/storage.py），以后可换成其他 S3 兼容存储。
# 源码包固定版本并校验 sha256；Go 依赖经 GOPROXY 下载并由 go.sum 校验。
# 构建机连不上外网时：MINIO_SRC_URL 指向公司内部镜像，GOPROXY 指向公司内部 Go 代理。
# MinIO 以 AGPL-3.0 发布，这里只在内部原样使用、不修改源码。

ARG GO_IMAGE=golang:1.26.11-bookworm
ARG RUNTIME_IMAGE=debian:bookworm-20261027-slim

FROM ${GO_IMAGE} AS builder
ARG MINIO_RELEASE=RELEASE.2025-10-15T17-29-55Z
ARG MINIO_COMMIT=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a
ARG MINIO_SRC_URL=https://github.com/minio/minio/archive/refs/tags/RELEASE.2025-10-15T17-29-55Z.tar.gz
ARG MINIO_SRC_SHA256=be6d0bd3696c3a13a35f02d3a0280b64319c67918b4501c5c3d87f96d000085c
ARG GOPROXY=https://proxy.golang.org,direct

WORKDIR /src
RUN set -eux; \
    curl -fsSL --retry 5 --retry-delay 10 --retry-all-errors "$MINIO_SRC_URL" -o minio.tar.gz; \
    echo "${MINIO_SRC_SHA256}  minio.tar.gz" | sha256sum -c -; \
    tar -xzf minio.tar.gz --strip-components=1; rm minio.tar.gz
# 发行版自带的部分 Go 依赖有已修复的漏洞（trivy 扫描，docs/安全扫描.md）：只升级这些依赖到修复版本，不改 MinIO 源码。
# 升级 MinIO 版本时先去掉这里的列表，按扫描结果重新确定。go get 改了 go.mod，构建用 -mod=mod 补齐 go.sum（仍经 GOSUMDB 校验）；
# 不做 go mod tidy（会拉取依赖的测试依赖，部分已不存在）。
ARG GO_DEP_FIXES="github.com/apache/thrift@v0.23.0 github.com/buger/jsonparser@v1.1.2 github.com/go-jose/go-jose/v4@v4.1.6 \
github.com/klauspost/compress@v1.18.7 github.com/prometheus/prometheus@v0.311.3 github.com/rabbitmq/amqp091-go@v1.13.0 \
go.opentelemetry.io/otel/sdk@v1.42.0 golang.org/x/crypto@v0.54.0 golang.org/x/text@v0.41.0 google.golang.org/grpc@v1.79.3"
RUN --mount=type=cache,target=/root/go/pkg/mod set -eux; \
    export GOPROXY="$GOPROXY" GOTOOLCHAIN=local CGO_ENABLED=0; \
    go get $GO_DEP_FIXES; \
    export GOFLAGS=-mod=mod; \
    go mod download; \
    version="$(echo "$MINIO_RELEASE" | sed -E 's/^RELEASE\.([0-9-]+)T([0-9]+)-([0-9]+)-([0-9]+)Z$/\1T\2:\3:\4Z/')"; \
    go build -tags kqueue -trimpath -o /out/minio -ldflags "-s -w \
      -X github.com/minio/minio/cmd.Version=${version} \
      -X github.com/minio/minio/cmd.ReleaseTag=${MINIO_RELEASE} \
      -X github.com/minio/minio/cmd.CommitID=${MINIO_COMMIT} \
      -X github.com/minio/minio/cmd.ShortCommitID=$(echo "$MINIO_COMMIT" | cut -c1-12) \
      -X github.com/minio/minio/cmd.CopyrightYear=2025"; \
    /out/minio --version

FROM ${RUNTIME_IMAGE}
RUN set -eux; \
    apt-get update; \
    apt-get upgrade -y --no-install-recommends; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --system --uid 10001 --home-dir /data minio; \
    mkdir -p /data && chown minio:minio /data
COPY --from=builder /out/minio /usr/local/bin/minio
USER minio
VOLUME ["/data"]
EXPOSE 9000
HEALTHCHECK --interval=10s --timeout=3s --retries=12 CMD curl -fsS http://127.0.0.1:9000/minio/health/live || exit 1
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--address", ":9000"]
