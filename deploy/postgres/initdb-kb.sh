#!/bin/sh
# 首次初始化数据卷时建知识区数据库和账号（M5 ③），见 kb-init.sh。没设 KB_POSTGRES_PASSWORD 时跳过（单元测试用的库）。
set -eu
if [ -z "${KB_POSTGRES_PASSWORD:-}" ]; then
  echo "未设置 KB_POSTGRES_PASSWORD，跳过知识区数据库初始化"
  exit 0
fi
exec /usr/local/bin/kb-init.sh
