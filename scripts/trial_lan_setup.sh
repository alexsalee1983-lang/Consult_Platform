#!/usr/bin/env bash
# 试用机开放给办公室内网的专家（负责人 2026-11-20：试用期专家都在同一办公室内网）。
# 做四件事：生成含本机内网 IP 的自签名证书、.env 设 COOKIE_SECURE=true 并取消只绑本机、
# 防火墙只放行网页端口（ufw 已启用时）、重启前端和后端并自检。不动数据卷，不写数据库。
#   bash scripts/trial_lan_setup.sh               # 自动取本机内网 IP
#   bash scripts/trial_lan_setup.sh 192.0.2.10    # 指定 IP
# 只用于试用机、测试机；生产按 docs/部署手册.md 用公司 CA 证书。
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "不通过：没有 .env，先按 docs/试用机部署.md 配置"; exit 1; }
if grep -qE '^APP_ENV=prod' .env; then echo "不通过：APP_ENV=prod，本脚本只用于试用机"; exit 1; fi

ip="${1:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
[ -n "$ip" ] || { echo "不通过：取不到内网 IP，请作为参数传入"; exit 1; }

getv() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
setv() {  # 改或追加 .env 一项
  if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '\n%s=%s\n' "$1" "$2" >> .env; fi
}

cp .env ".env.bak-lan-$(date +%Y%m%d-%H%M%S)"
setv COOKIE_SECURE true
if [ "$(getv WEB_BIND)" = "127.0.0.1" ]; then sed -i 's|^WEB_BIND=127.0.0.1|# WEB_BIND=127.0.0.1（trial_lan_setup 取消：内网专家访问）|' .env; fi
web_port="$(getv WEB_PORT)"; web_port="${web_port:-8080}"
tls_port="$(getv WEB_TLS_PORT)"; tls_port="${tls_port:-8443}"
echo "通过：.env 已改（COOKIE_SECURE=true，监听所有网卡），原文件已备份"

tls_dir="$(getv TLS_DIR)"
TLS_DIR="${tls_dir:-deploy/tls}" bash scripts/gen_test_cert.sh "$ip"

if command -v ufw >/dev/null 2>&1 && sudo -n ufw status 2>/dev/null | grep -q "Status: active"; then
  sudo -n ufw allow "$tls_port/tcp" >/dev/null
  sudo -n ufw allow "$web_port/tcp" >/dev/null   # http 只做跳转到 https
  echo "通过：防火墙已放行 $tls_port、$web_port"
else
  echo "提示：ufw 未启用或无免密 sudo，防火墙未改。需要时手动执行：sudo ufw allow $tls_port/tcp"
fi

docker compose up -d frontend backend worker >/dev/null
for i in $(seq 1 30); do
  if curl -sk "https://127.0.0.1:$tls_port/api/health" | grep -q '"ok"\|"status"'; then break; fi
  sleep 5
done
if curl -sk "https://$ip:$tls_port/api/health" >/dev/null; then
  echo "通过：https://$ip:$tls_port 可访问"
else
  echo "不通过：https://$ip:$tls_port 访问不到，看 docker compose logs frontend"; exit 1
fi
echo "下一步：CURL_CA_BUNDLE=${TLS_DIR:-${tls_dir:-deploy/tls}}/server.crt bash scripts/smoke_test.sh https://localhost:$tls_port；专家浏览器打开 https://$ip:$tls_port（自签名证书点一次“继续访问”）"
