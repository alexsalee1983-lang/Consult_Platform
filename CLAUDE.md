# CLAUDE.md · 安全咨询 Agent 平台

本文件给在本仓库工作的 Claude Code（开发助手、测试助手）读。开始任何工作前先读完它，再读 `docs/开发计划.md`。

## 项目是什么

公司安全咨询团队的内部平台：顾问与 Agent 组协作完成咨询交付件，平台接入**公司私有化部署的白名单大模型**，并维护公司知识库。一期目标：2027-01-15 交付第一个可用版本。

当前进度和下一步看 `docs/开发计划.md`。验收标准以该文件第三节为准。

## 资料索引（规划与设计都在仓库里，不需要也无法访问 claude.ai）

| 要做什么 | 先读 |
|---|---|
| 开始一个冲刺 | `docs/开发计划.md` |
| 设计数据表、权限、网关、部署 | `docs/架构设计.md`（第三节客户数据保护、第五节工具架构与 Agent × 工具授权矩阵、第七节安全） |
| 遇到取舍 | `docs/决策记录.md`，已拍板的不要推翻；没覆盖到的问负责人 |
| 写界面 | `design/README.md` 和 `design/screens/*.html`（浏览器可直接打开），配色只用 `frontend/src/theme.css` 令牌 |
| Agent 运行时、提示词 | `agents/01_公共规范.md`（所有 Agent 的公共前缀）、`agents/00_总体设计.md`、`agents/roles/*.md` |
| 知识服务（M2） | `docs/知识库建设方案.md`、`docs/知识库建设总体计划.md`、`docs/Agent组开发项目方案.md` 第十二节 |
| 交付链（M3） | `docs/Agent组开发项目方案.md`（D1～D8、交付链 7 步、确认卡） |
| 删除、告知、严格档（M4） | `docs/客户数据告知模板与合同条款.md`：这是对客户的承诺，平台功能必须能兑现 |
| 测试与回报 | `docs/测试交接规范.md` |

`agents/` 下的 md 是 Agent 组的正式版本，修改须经负责人同意（`01_公共规范.md` 的修改影响所有 Agent）。

## 分工

| 角色 | 在哪里 | 职责 |
|---|---|---|
| 开发助手 | 开发电脑上的 Claude Code | 按开发计划写代码和测试，推分支，开 PR |
| 测试助手 | Ubuntu 测试机上的 Claude Code | 拉取 PR 分支，用 Docker 跑起来测试，按 `docs/测试交接规范.md` 把结果写成 PR 评论 |
| 规划与审阅 | claude.ai 对话 | 维护计划、架构、设计稿；按验收标准审阅 PR |
| 负责人 | 用户本人 | 拍板、合并 PR |

## 数据保护红线（违反任何一条都不能合并）

1. **代码仓、测试数据、截图、日志、提交信息里不得出现真实客户信息。** 只用虚拟客户（BANK-A、GRID-B、MFG-C 等）。
2. **所有大模型调用必须经过 `backend/app/services/gateway.py`。** 其他模块不得直接请求模型地址，不得引入会联网调用外部 AI 或外部服务的库。外部模型只允许在开发和测试环境、只处理虚拟数据时经网关使用（`LLM_EXTERNAL=true`，决策记录 2026-11-08），网关对外部模型强制假名化、拒绝严格档；生产环境开启即拒绝启动。
3. **发往模型和进入平台的内容先过机密级检测**（`guard`）。机密级包括：口令、密钥、可利用的漏洞细节、完整网络拓扑与 IP 规划、业务数据和个人信息样本。两档项目都不收。
4. **审计日志和应用日志不记客户原文**，只记长度、摘要哈希、命中类别。
5. **严格档项目**：资料不出客户现场，平台只接收客户书面同意后的去标识摘要卡；Agent 输出一律用占位符。
6. **权限隔离**：非项目成员看不到项目（返回 404）；知识侧 Agent 不能读项目区；交付侧 Agent 不能写知识库。改动权限相关代码必须同步更新权限矩阵测试。
7. **密钥只放 `.env` 或部署机的密钥文件**，不提交；`.env.example` 只放占位值。

## 资质边界

平台和 Agent 不得出具或暗示等保测评结论、密评结论、认证结论。界面和提示词统一用“差距分析、符合性初判、整改建议”。

## 技术约定

- 后端：Python 3.12、FastAPI、SQLAlchemy 2、Pydantic 2；全部带类型注解；`ruff check` 必须通过。
- 前端：React 18 + Vite；颜色只用 `frontend/src/theme.css` 的暗色版令牌，不写硬编码颜色；界面文案用中文。
- 依赖：版本全部固定；新增依赖在 PR 里说明理由。
- 数据库：S1 起所有表结构变更只通过 Alembic 迁移脚本。
- 目录：S1 起逐步调整到 `docs/开发计划.md` 第五节的结构。
- 界面设计以 `design/screens/` 的暗色版 6 屏为准（顾问对话、我的项目、项目驾驶舱、交付件审阅、知识审批队列、知识地图与健康）。

## 常用命令

```bash
# 后端测试（本地）
cd backend && pip install -r requirements-dev.txt && ruff check app tests alembic && pytest -q
# 与 CI 一致的 PostgreSQL 测试（平台数据库镜像，含 zhparser；只在有 Docker 的机器上）
docker build -t consult-platform-postgres:ci deploy/postgres
docker run -d --name testdb -e POSTGRES_DB=consult_test -e POSTGRES_USER=consult -e POSTGRES_PASSWORD=consult -p 127.0.0.1:5432:5432 consult-platform-postgres:ci
TEST_DATABASE_URL=postgresql+psycopg://consult:consult@127.0.0.1:5432/consult_test pytest -q

# 数据库迁移（backend 目录下；后端启动时也会自动升级到最新）
python -m app.core.migrate
alembic revision --autogenerate --rev-id 0002_xxx -m "M1：说明"   # 生成后人工检查

# 前端构建
cd frontend && npm install && npm run build

# 整套启动与冒烟测试（测试机）
cp .env.example .env   # 首次；按注释配置 COOKIE_SECURE 和测试账号 TEST_ACCOUNT_*
docker compose up -d --build
bash scripts/smoke_test.sh
docker compose logs --no-color backend | tail -100

# 用户管理：日常在网页“用户与角色”页操作（系统管理员）；以下命令行用于首个管理员、审计员任命和应急（口令交互输入）
docker compose exec backend python -m app.cli create-user --username <名字> --display-name <姓名>
docker compose exec backend python -m app.cli unlock --username <名字>
docker compose exec backend python -m app.cli reset-totp --username <名字>
docker compose exec backend python -m app.cli grant-role --username <名字> --role consultant   # 审计员只能这样授予
docker compose exec backend python -m app.cli list-users
docker compose exec backend python -m app.cli verify-audit          # 校验审计哈希链（问题时退出码 2）
bash scripts/audit_chain_check.sh                                    # 测试机：多进程并发写审计后校验（生产拒绝执行）
docker compose exec backend python -m app.cli usage-report --days 30 # 大模型用量（按人、项目、模型）

# 没有公司模型时，用模拟模型跑通网关真实路径（.env 设 LLM_BASE_URL=http://mock-llm:8000/v1）
docker compose --profile mock-llm up -d --build
SMOKE_EXPECT_LLM=ok bash scripts/smoke_test.sh
```

工具约定：Agent 读写数据只经 `app/agents/tools.py` 的 `invoke()`（授权 + 硬规则 + 项目成员检查 + 审计）。Agent × 工具授权由迁移写入，改授权要新迁移、同步 `tests/test_tools.py` 的 `EXPECTED`，并在 PR 写明依据；不提供修改授权的接口。新工具要在 `tools` 表登记区域（knowledge / project / staging / none）和读写，硬规则按这两项判断。

运行时约定：Agent 的系统提示 = `agents/01_公共规范.md` + 角色 md 正文 + 平台生成的项目卡（`app/agents/runtime.py`）；项目卡经工具层 `project.read` 读取。md 的 frontmatter 只读 `name`（必须与 Agent 代号一致）、`model_tier`、`tools`（仅作说明）、`service_modules`。每条对话和回复记下提示词版本（md 内容哈希）。后端镜像以仓库根目录为构建范围，`agents/` 打进镜像（`backend/Dockerfile`、根目录 `.dockerignore`）；测试机可只读挂载 `./agents:/srv/agents:ro`，md 改了自动重新加载。

网关约定：大模型调用只经 `app/services/gateway.py` 的 `complete()` / `stream()`；调用方负责写审计和用量（`services/usage.record`），都不记提问和回复原文。测试里用 `gateway.set_transport(httpx.MockTransport(...))` 模拟模型，不要连真实地址。

对象存储约定：文件只经 `app/services/storage.py` 读写（S3 协议，一期为自建 MinIO，见 `deploy/minio/README.md`）；只有 project（项目区）和 knowledge（知识区）两个区，分桶存放、默认服务端加密；哪个 Agent 能碰哪个区由工具层硬规则判断。测试里设 `TEST_S3_ENDPOINT` 才连真实存储，否则相关测试跳过。

审计约定：审计只经 `app/core/audit.record()` 写入（只追加的哈希链，写入串行化）；不得 UPDATE / DELETE `audit_logs`（数据库触发器会拒绝）。以后的迁移如果要在 SQLite 上用 batch 模式改 `audit_logs`，要重建触发器。

登录约定：默认所有接口都要登录，公开接口只有 `/api/health` 和 `/api/auth/login`（白名单在 `app/core/auth.py`）；新增接口必须加进 `tests/test_auth.py` 的 `PROTECTED` 列表，否则测试失败。修改类请求要带 `X-CSRF-Token`。管理员在网页上建的账号或重置过口令的账号（`must_change_password`），改口令前只能访问 `PASSWORD_CHANGE_PATHS`（同一文件）。

权限约定（规则全文在 `app/core/permissions.py` 文件头）：业务接口在路由上挂 `require_business`；按项目取数据一律用 `project_for_member`（非成员 404）；系统管理员、安全审计员与其他角色互斥且看不到业务数据；审计员只能用命令行授予。新增或改动接口权限，必须同步更新 `tests/test_permissions.py` 的权限矩阵。

## 分支、提交与 PR

- 每个里程碑一个分支，例如 `feat/m1-auth`；一个 PR 只做一件事，便于审阅和回退。
- 提交信息用中文，以里程碑开头，例如 `M1：登录与会话`。
- PR 描述必须写：改了什么、怎么测（测试助手照着执行）、对应开发计划哪一项、是否涉及数据库迁移或权限变更。
- 合并条件：CI 全绿 → 测试助手在测试机上测试通过并在 PR 留评论 → 负责人合并。
- 每个里程碑验收后打标签 `v0.1`、`v0.2`……，一期可用版本为 `v1.0`。

## 遇到以下情况先停下来问负责人

- 需要改动上面的数据保护红线或资质边界
- 需要引入新的外部服务或联网依赖
- 需要删除数据、重写迁移历史、强制推送
- 开发计划里标为“待决定”的事项（SSO 方式、模型接口、对象存储、Word 模板、现场工具形态）
