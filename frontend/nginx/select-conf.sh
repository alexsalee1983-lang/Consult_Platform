#!/bin/sh
# nginx 官方镜像启动前执行 /docker-entrypoint.d/ 下的脚本：按是否挂载了证书选择 HTTP 或 HTTPS 配置。
# 生产环境（APP_ENV=prod）必须用 HTTPS：没有证书时拒绝启动。
set -eu
crt=/etc/nginx/tls/server.crt
key=/etc/nginx/tls/server.key
if [ -s "$crt" ] && [ -s "$key" ]; then
    port="${WEB_TLS_PORT:-443}"
    case "$port" in *[!0-9]*|"") echo "WEB_TLS_PORT 不是端口号：$port" >&2; exit 1;; esac
    suffix=""
    [ "$port" = "443" ] || suffix=":$port"
    sed "s|__HTTPS_PORT_SUFFIX__|$suffix|" /etc/nginx/sites/https.conf > /etc/nginx/conf.d/default.conf
    echo "前端：HTTPS 已启用（证书 $crt，跳转端口 ${port}）"
else
    if [ "${APP_ENV:-dev}" = "prod" ]; then
        echo "生产环境（APP_ENV=prod）必须启用 HTTPS：请把证书放到 TLS_DIR 下的 server.crt 和 server.key（见 .env.example 的 TLS_DIR）" >&2
        exit 1
    fi
    cp /etc/nginx/sites/http.conf /etc/nginx/conf.d/default.conf
    echo "前端：只用 HTTP（没有挂载证书；仅限开发机、测试机）"
fi
