# 平台数据库镜像：PostgreSQL 16 + pgvector（向量检索）+ zhparser（中文分词，全文检索用）。
#
# zhparser 和它依赖的 SCWS 分词库没有现成的 Debian 包，这里从源码编译。
# 源码包固定版本并校验 sha256；构建机连不上外网时，可用 SCWS_URL / ZHPARSER_URL 指向公司内部镜像。
# 升级版本时同时改版本号和 sha256，并在 PR 里说明。

ARG BASE_IMAGE=pgvector/pgvector:0.8.6-pg16-bookworm

FROM ${BASE_IMAGE} AS builder
ARG SCWS_URL=http://www.xunsearch.com/scws/down/scws-1.2.3.tar.bz2
ARG SCWS_SHA256=60d50ac3dc42cff3c0b16cb1cfee47d8cb8c8baa142a58bc62854477b81f1af5
ARG ZHPARSER_URL=https://github.com/amutu/zhparser/archive/refs/tags/v2.3.tar.gz
ARG ZHPARSER_SHA256=96fe19f1b72c922eab19ecfc305384381562ff840da4d3b77452e21fefe8f0d2
ARG DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential "postgresql-server-dev-${PG_MAJOR}" curl ca-certificates bzip2; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN set -eux; \
    curl -fsSL --retry 5 --retry-delay 10 --retry-all-errors "$SCWS_URL" -o scws.tar.bz2; \
    echo "${SCWS_SHA256}  scws.tar.bz2" | sha256sum -c -; \
    mkdir scws && tar -xjf scws.tar.bz2 -C scws --strip-components=1; \
    cd scws && ./configure --prefix=/usr/local && make -j"$(nproc)" && make install
RUN set -eux; \
    curl -fsSL --retry 5 --retry-delay 10 --retry-all-errors "$ZHPARSER_URL" -o zhparser.tar.gz; \
    echo "${ZHPARSER_SHA256}  zhparser.tar.gz" | sha256sum -c -; \
    mkdir zhparser && tar -xzf zhparser.tar.gz -C zhparser --strip-components=1; \
    cd zhparser && SCWS_HOME=/usr/local make -j"$(nproc)" && SCWS_HOME=/usr/local make install

FROM ${BASE_IMAGE}
COPY --from=builder /usr/local/lib/libscws.so* /usr/local/lib/
COPY --from=builder /usr/lib/postgresql/${PG_MAJOR}/lib/zhparser.so /usr/lib/postgresql/${PG_MAJOR}/lib/
COPY --from=builder /usr/share/postgresql/${PG_MAJOR}/extension/zhparser* /usr/share/postgresql/${PG_MAJOR}/extension/
COPY --from=builder /usr/share/postgresql/${PG_MAJOR}/tsearch_data/dict.utf8.xdb /usr/share/postgresql/${PG_MAJOR}/tsearch_data/
# 打上基础镜像发布后的系统安全更新（trivy 扫描，docs/安全扫描.md）
RUN set -eux; apt-get update; apt-get upgrade -y --no-install-recommends; rm -rf /var/lib/apt/lists/*; ldconfig
# 知识区数据库和账号（M5 ③ 分库）：新数据卷自动建；已有数据卷执行 docker compose exec postgres kb-init.sh
COPY --chmod=755 kb-init.sh /usr/local/bin/kb-init.sh
COPY --chmod=755 initdb-kb.sh /docker-entrypoint-initdb.d/20-kb-init.sh
