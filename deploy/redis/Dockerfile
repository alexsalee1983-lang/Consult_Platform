# redis:7-alpine 基于 alpine 3.22，基础镜像里 libcrypto3/libssl3 有 HIGH 漏洞（CVE-2026-124709，修复版本 3.5.9-r2）。
# Redis 7.x 暂无 alpine3.23 标签，故在官方镜像上只升级这两个包；官方镜像重建后可删除本目录改回 image。
FROM redis:7-alpine
RUN apk upgrade --no-cache libssl3 libcrypto3
