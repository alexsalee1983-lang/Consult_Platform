#!/bin/sh
# 首次初始化数据卷时建主库应用账号（M5 ⑧），见 app-init.sh。没设 APP_POSTGRES_PASSWORD 时跳过（单元测试用的库）。
set -eu
if [ -z "${APP_POSTGRES_PASSWORD:-}" ]; then
  echo "未设置 APP_POSTGRES_PASSWORD，跳过主库应用账号初始化"
  exit 0
fi
exec /usr/local/bin/app-init.sh
