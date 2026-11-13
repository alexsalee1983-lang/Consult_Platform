#!/usr/bin/env bash
# 测试机用的自签名证书（不要用于生产）：写到 TLS_DIR（默认 deploy/tls），证书含 localhost、127.0.0.1 和参数里的主机名或 IP。
#   bash scripts/gen_test_cert.sh                 # 只含 localhost
#   bash scripts/gen_test_cert.sh 192.0.2.10      # 再加测试机地址
# 生成后 docker compose up -d frontend（或 restart frontend）即启用 HTTPS。
set -euo pipefail
cd "$(dirname "$0")/.."
dir="${TLS_DIR:-deploy/tls}"
mkdir -p "$dir"
san="DNS:localhost,IP:127.0.0.1"
for h in "$@"; do
  if [[ "$h" =~ ^[0-9.]+$ || "$h" == *:* ]]; then san="$san,IP:$h"; else san="$san,DNS:$h"; fi
done
umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 90 \
  -subj "/CN=consult-platform-test" -addext "subjectAltName=$san" \
  -keyout "$dir/server.key" -out "$dir/server.crt" 2>/dev/null
chmod 644 "$dir/server.crt"
# nginx 主进程以 root 读取私钥；仍只让文件属主可读
chmod 600 "$dir/server.key"
echo "已生成自签名证书：$dir/server.crt（$san，90 天）"
