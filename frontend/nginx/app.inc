# HTTP 和 HTTPS 两种监听共用的站点配置（由 nginx/select-conf.sh 选一种监听方式后 include 本文件）。
root /usr/share/nginx/html;
client_max_body_size 50m;
server_tokens off;

# 安全响应头（等保自查 ②a，docs/等保三级技术控制自查.md）。
# CSP：只加载本站脚本和样式，禁止被嵌入、禁止插件；页面不引用任何外部字体、脚本（内网部署）。
# style-src 允许 'unsafe-inline'：界面组件用 style 属性排版；脚本仍只允许本站文件。
add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'" always;
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header Referrer-Policy same-origin always;
add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=()" always;
add_header Cross-Origin-Opener-Policy same-origin always;

# 用 Docker 内置 DNS 每 10 秒重新解析 backend：后端容器重建换了 IP 后，不用重启前端也能连上。
# proxy_pass 写成变量，nginx 才会在运行时按 resolver 解析（写死主机名只在启动时解析一次）。
resolver 127.0.0.11 valid=10s ipv6=off;
set $backend_upstream http://backend:8000;

location /api/ {
    proxy_pass $backend_upstream;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 180s;
}

location / {
    try_files $uri /index.html;
}
