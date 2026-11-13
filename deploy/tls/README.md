# TLS 证书目录

前端（nginx）启动时，这个目录（或 `.env` 的 `TLS_DIR` 指向的目录）里有 `server.crt` 和 `server.key` 就启用 HTTPS：

- `WEB_TLS_PORT`（生产为 443）提供服务，`WEB_PORT` 只做 301 跳转；响应带 HSTS；只允许 TLS 1.2 / 1.3。
- 没有证书时只用 HTTP，仅限开发机和测试机；`APP_ENV=prod` 没有证书时前端拒绝启动。

生产：用公司 CA 签发的证书，`server.crt` 里依次放服务器证书和中间证书；私钥文件权限 600，只放部署机，不提交。
换证书后 `docker compose restart frontend`。

测试机：`bash scripts/gen_test_cert.sh [额外主机名或 IP]` 生成自签名证书（有效期 90 天），浏览器会提示不受信任，
冒烟测试用 `CURL_CA_BUNDLE=deploy/tls/server.crt bash scripts/smoke_test.sh https://localhost:8443`。

本目录除本说明外的文件都不提交（`.gitignore`）。
