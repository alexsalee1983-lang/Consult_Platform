FROM node:20-alpine AS build
WORKDIR /web
# 国内网络可传入镜像源：--build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=https://registry.npmjs.org
COPY package.json package-lock.json ./
# 只按锁文件安装，保证每次构建的依赖版本一致
RUN npm config set registry ${NPM_REGISTRY} && npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
# 站点配置：启动时由 select-conf.sh 按是否挂载证书选 HTTP 或 HTTPS（生产必须 HTTPS）
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx/app.inc /etc/nginx/app.inc
COPY nginx/http.conf nginx/https.conf /etc/nginx/sites/
COPY --chmod=0755 nginx/select-conf.sh /docker-entrypoint.d/40-select-conf.sh
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80 443
