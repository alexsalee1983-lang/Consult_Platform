# 安全咨询 Agent 平台（Consult_Platform）

面向公司安全咨询顾问的内部平台：顾问在平台上与 Agent 组协作完成交付件，平台接入公司私有化部署的白名单大模型，并维护公司知识库。

当前版本 **M0：可运行骨架**。

| 已有 | 说明 |
|---|---|
| 暗色版界面 3 屏 | 我的项目、项目驾驶舱、顾问对话 |
| 项目与交付件 | 项目、数据档位（标准档 / 严格档）、D1～D8 交付件、待确认事项；带虚拟客户演示数据 |
| 机密级拦截 | 口令、密钥、私钥、云访问密钥、IP 与网段、身份证号、银行卡号、手机号、邮箱，命中即拦截 |
| 模型网关 | 所有大模型调用的唯一出口；按推理 / 标准 / 轻量路由；严格档自动加占位符要求；未配置模型时为演示模式 |
| 审计日志 | 记录谁、何时、对哪个项目做了什么；只记长度和摘要哈希，不记原文 |
| 基础设施 | PostgreSQL 16 + pgvector、Redis、Nginx（对象存储 M1 加入） |

## 在 Ubuntu 上部署和测试

前提：已安装 Docker Engine 24 以上（含 `docker compose` 插件）和 git。

```bash
# 1. 下载代码
git clone https://github.com/alexsalee1983-lang/Consult_Platform.git
cd Consult_Platform
git checkout feat/m0-skeleton        # 合并到 main 之后可以省略这一步

# 2. 配置
cp .env.example .env
nano .env                            # 至少修改 POSTGRES_PASSWORD；
                                     # 有模型地址时填 LLM_BASE_URL，没有就留空（演示模式）

# 3. 构建并启动（首次需要下载镜像和依赖，约 5～10 分钟）
docker compose up -d --build
docker compose ps                    # 等 backend 显示 healthy

# 4. 冒烟测试
bash scripts/smoke_test.sh           # 7 项全部“通过”即正常

# 5. 打开浏览器
# http://<服务器 IP>:8080
```

国内网络下载慢时，在 `.env` 里取消注释 `PIP_INDEX_URL` 和 `NPM_REGISTRY` 两行，再执行第 3 步。

### 常用命令

```bash
docker compose logs -f backend       # 看后端日志
docker compose restart backend       # 改了 .env 之后重启后端
git pull && docker compose up -d --build   # 更新到最新代码
docker compose down                  # 停止（保留数据）
docker compose down -v               # 停止并清空所有数据（慎用）
```

### 端口

| 端口 | 用途 | 访问范围 |
|---|---|---|
| 8080 | 平台网页和接口 | 对内网开放 |
| 8000 | 后端接口调试，`http://127.0.0.1:8000/docs` 可看接口文档 | 仅本机 |

## 目录

```
backend/      FastAPI 后端：app/guard.py 机密级检测，app/gateway.py 模型网关，app/routers 接口
frontend/     React + Vite 前端，暗色版设计规范在 src/theme.css
scripts/      冒烟测试
.github/      CI：后端测试、前端构建、整套 Docker 启动后跑冒烟测试
```

## 开发

```bash
# 后端
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
DATABASE_URL=sqlite:///./dev.db uvicorn app.main:app --reload

# 前端（另开终端，/api 自动转发到 8000）
cd frontend && npm install && npm run dev
```

## 数据保护约定

- 代码仓里**不得**出现任何真实客户资料；演示数据均为虚拟客户。
- `.env` 含口令，不提交。
- 机密级内容两档都不收；严格档项目只接收客户书面同意后的去标识摘要卡。

## 待决定

- **对象存储选型**：MinIO 社区版官方镜像已于 2026 年 9 月从 Docker Hub 下架（只提供源码）。M1 做文档上传前需要在以下方案中选一个：SeaweedFS（S3 兼容，开源维护活跃）、用源码自建 MinIO 镜像、Chainguard 的 MinIO 镜像（免费版只有 latest 标签）、或一期先用服务器本地加密目录。

## 路线

- **M0**（当前）：骨架、机密级拦截、模型网关、审计、3 屏界面
- **M1**：SSO 与权限（RBAC、项目隔离）、对象存储、文档上传与解析、项目区保留与人工确认删除
- **M2**：知识库（入库、检索 pgvector + 中文分词、审批与双人审核）、Agent 编排（LangGraph）
- **M3**：交付件生成与审阅、质量门禁、Word 导出
