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
docker compose exec backend python -m app.cli add-member --project <项目> --username <名字> --role manager   # 没有项目经理的项目指定首位经理、应急；日常由项目经理在网页调整
docker compose exec backend python -m app.cli create-project --code <客户代号> --tier standard --manager <名字> [--industry 银行]   # 立项（一期无网页立项）：必须定档，建 D1～D8，指定首位项目经理
docker compose exec backend python -m app.cli retention-report      # 已验收项目的保留期与到期状态（--all 含未验收）
docker compose exec backend python -m app.cli deletion-scan         # 立即扫描到期项目并发起删除申请（worker 每小时自动扫描）
docker compose exec backend python -m app.cli deletion-report       # 未完成的删除申请（--all 含已删除、已撤销）
docker compose exec backend python -m app.cli verify-audit          # 校验审计哈希链（问题时退出码 2）
bash scripts/audit_chain_check.sh                                    # 测试机：多进程并发写审计后校验（生产拒绝执行）
docker compose exec backend python -m app.cli usage-report --days 30 # 大模型用量（按人、项目、模型）

# 知识批量入库与评测（条目文件格式见 backend/app/kb/files.py，示范见 knowledge/）
docker compose exec backend python -m app.cli kb-import /srv/knowledge/samples --as <知识专家> [--submit]
docker compose exec backend python -m app.cli kb-draft --source <公开资料> --source-detail "<来源>" --out /tmp/drafts --as <知识专家>
docker compose exec backend python -m app.cli kb-eval /srv/knowledge/eval/samples.yaml --as <用户> [--out /tmp/report.json]

# 没有公司模型时，用模拟模型跑通网关真实路径（.env 设 LLM_BASE_URL=http://mock-llm:8000/v1）
docker compose --profile mock-llm up -d --build
SMOKE_EXPECT_LLM=ok bash scripts/smoke_test.sh
```

工具约定：Agent 读写数据只经 `app/agents/tools.py` 的 `invoke()`（授权 + 硬规则 + 项目成员检查 + 审计）。Agent × 工具授权由迁移写入，改授权要新迁移、同步 `tests/test_tools.py` 的 `EXPECTED`，并在 PR 写明依据；不提供修改授权的接口。新工具要在 `tools` 表登记区域（knowledge / project / staging / none）和读写，硬规则按这两项判断。

运行时约定：Agent 的系统提示 = `agents/01_公共规范.md` + 角色 md 正文 + 平台生成的项目卡（`app/agents/runtime.py`）；项目卡经工具层 `project.read` 读取。md 的 frontmatter 只读 `name`（必须与 Agent 代号一致）、`model_tier`、`tools`（仅作说明）、`service_modules`。每条对话和回复记下提示词版本（md 内容哈希）。后端镜像以仓库根目录为构建范围，`agents/` 打进镜像（`backend/Dockerfile`、根目录 `.dockerignore`）；测试机可只读挂载 `./agents:/srv/agents:ro`，md 改了自动重新加载。

网关约定：大模型调用只经 `app/services/gateway.py` 的 `complete()` / `stream()`；调用方负责写审计和用量（`services/usage.record`），都不记提问和回复原文。测试里用 `gateway.set_transport(httpx.MockTransport(...))` 模拟模型，不要连真实地址。

对象存储约定：文件只经 `app/services/storage.py` 读写（S3 协议，一期为自建 MinIO，见 `deploy/minio/README.md`）；只有 project（项目区）和 knowledge（知识区）两个区，分桶存放、默认服务端加密；哪个 Agent 能碰哪个区由工具层硬规则判断。测试里设 `TEST_S3_ENDPOINT` 才连真实存储，否则相关测试跳过。

后台任务约定：耗时操作（解析、向量化、到期删除）不在请求里做，用 `app/services/jobs.py` 的 `enqueue()` 排队，由 worker 容器（`python -m app.worker`）执行；处理器用 `@jobs.handler("种类")` 注册，必须可重复执行；任务参数只放编号和对象键，不放原文。运维：`python -m app.cli jobs`（状态）、`python -m app.cli selfcheck`（自检）。

知识库约定：知识区表结构在 `app/models/kb.py`，元数据取值和校验在 `app/kb/meta.py`（依据公共规范第 7 节，改取值要同步公共规范并经负责人同意）；建条目、建版本只经 `app/kb/items.py`。条目的 doc_id 固定不变，内容变化一律建新版本，历史版本保留。知识区只收去标识后的做法：去标识报告只记类别、占位符和次数，上传记录只记机密级命中类别，都不记原文。去标识只经 `app/kb/deid_flow.py`：规则词库（`deid.py`）→ 脱敏审查 Agent 复查（`deid_agent.py`，经网关，返回的漏网名称只在内存里用）→ 知识管理员确认（不能是上传者）；确认后原件和未去标识的解析结果从对象存储删除。发布审核按两人原则（`app/kb/review.py`，决策记录 2026-11-13）：作者不能审自己的版本；内容审核要知识专家、合规审核要知识管理员，一人持两个角色可以审两项；`KB_SINGLE_REVIEWER_PUBLIC=true` 时公开资料可由作者本人审核发布，标记 `single_review`，作者以外的知识专家复审确认后清除。

检索约定：知识检索只经 `app/kb/search.py`（全文 + 向量，RRF 融合），只查已发布、未下线、调用人看得到的现行版本；Agent 经工具 `kb.search`、`basis.lookup` 调用。重排只经网关 `gateway.rerank()`（tools/rerank 服务，默认 bge-reranker-v2-m3 int8；查询过机密级检测，命中的候选不发出、记 0 分），RRF 后前 `KB_RERANK_CANDIDATES` 个切片按重排分数排序，依据查询门槛用 `KB_BASIS_MIN_RERANK`；重排不可用时退回 RRF 排序和余弦相似度门槛，返回体 `rerank` 字段注明。查询写了标准号或法规条号时先按 L4 元数据精确匹配（`search._exact_match`）。向量化只经网关 `gateway.embed()`（每条先过机密级检测）；向量列维度 512 写在迁移 0014 和 `gateway.EMBED_DIM`，换维度不同的模型要新迁移并执行 `python -m app.cli kb-reembed`。

项目区约定（M3）：客户资料、访谈纪要、现场观察只经 `app/project/evidence.py` 进入平台（先过机密级检测；严格档一律拒收），按项目分配证据编号 D-xx / I-xx / O-xx，编号不变、不复用；访谈纪要只写受访者角色。资料原件和解析结果存对象存储项目区，键里不用原文件名；文件名和正文不进审计。Agent 经工具 `project.evidence` 读证据。仅现场使用（M4 ③）：项目经理凭客户书面通知把资料 D-xx 标为仅现场使用（`POST /api/projects/{code}/evidences/{ev}/local-only`，写文号不写客户名，不可撤销），平台立即删除对象存储里的原件和解析结果、清空文件名，编号和 sha256 保留；同一文件再上传 422，`project.evidence` 读不到，交付件写入引用 422，已有引用在签署门禁和质量评审平台规则里判阻断；审计不记文号。

交付件约定（M3）：交付件按章节存储，只经 `app/project/deliverables.py` 写入（Agent 经工具 `doc.generate`，顾问经 `/api/projects/{code}/deliverables/...`）：先写大纲再写各章，每次写入新建一个版本、旧版本保留（`section_versions` 只追加）；正文和标题先过机密级检测；严格档只收占位符版本（M4 ④b：`cards.placeholder_problems` 查机构名、人名、联系方式、网络标识、系统名、原话，命中 422 不保存）、已签署拒绝；正文里的来源标注 `[I-01]` `[D-12]` `[O-05]` `[S-02]`（严格档摘要卡）`[L4-…]` `[推断]` 写入时核对并记在版本的 `sources` 里，核对不上的不拒绝、列入 `unresolved`。审计只记编号、版本、字数和摘要哈希，不记正文和标题。

确认卡约定（M3）：Agent 遇到方案取舍、来源冲突、证据不足、范围确认、入库确认，经工具 `confirm.create` 发卡（选项、推荐和理由、影响，可关联交付件某章），规则只在 `app/project/confirmations.py`：卡片文字过机密级检测；同一 Agent 重复发同标题的未处理卡不新建；顾问经 `POST /api/confirmations/{id}/resolve` 选选项（`choice`）或写意见（`resolution`），处理过的不能再处理。审计不记标题、选项和意见原文。

导出约定（M3 ⑥）：交付件 Word 导出只经 `app/project/export.py`（`GET /api/projects/{code}/deliverables/{dcode}/export`）：模板从 `EXPORT_TEMPLATE` 读（不配用内置默认模板；配了但打不开返回 503，不退回默认），占位符和 `{{正文}}` 锚点见文件头；未签署的每页页眉标草稿，页脚写导出人、时间和导出编号；来源标注保留原样并加 Word 脚注（python-docx 不支持脚注，直接写 `word/footnotes.xml`）；严格档导出占位符版本（M4 ④b）、没有大纲 409。审计只记编号、各章版本号、文件大小和哈希、模板类别和导出编号，不记正文、标题和模板路径。

质量门禁与签署约定（M3 ⑤）：质量评审和签署只经 `app/project/quality.py`（接口 `/api/projects/{code}/deliverables/{dcode}/reviews`、`.../signoff`）。评审是后台任务 `quality.review`：平台硬规则（缺正文、机密级、测评 / 认证结论用语、来源核对不上 → 阻断；整章只有推断 → 重要）+ 质量评审 Agent 经网关评审（平台按发起人身份把发起时各章版本的正文交给它，不新增工具授权），结论由平台按 `qa-reviewer.md` 的规则重算（有阻断不通过、重要 ≥3 修改后通过）。门禁：每章有正文、最近一次评审针对当前修订且未驳回的阻断为 0；项目经理可写理由（去空白至少 10 字）逐条驳回评审 Agent 提出的阻断（`review_waivers`，只追加，接口 `.../reviews/{rid}/waivers`），平台规则判出的阻断不能驳回；严格档另有平台规则“占位符”（正文出现真名、网络标识、系统名、原话 → 阻断）。流程：项目成员提交签署（review）→ 项目经理签署（signed，可签自己提交的）或退回（必须写理由，回 drafting）；待签署时改正文自动回 drafting；签署后只读、导出不带草稿页眉，一期不提供撤销。`sign_offs` 只追加。审计只记编号、修订号、版本号、计数和结论，不记正文、问题描述、意见和驳回理由。

编排约定（M3 ④）：交付链运行只经 `app/project/runs.py`（平台自写流程控制，不用 LangGraph）：标准档 3→4→7（调研访谈 Agent 读证据写 D3 → 依据与差距分析 Agent 出差距分析、存运行记录 → 报告与汇报 Agent 写 D4 初稿），每步是后台任务 `agent_run.step`，开头看运行状态、不是 queued 就不做；Agent 读写仍只经 `tools.invoke`，模型只经 `gateway.complete`。没有证据发“证据不足”卡停在本步；模型输出里的确认卡照发、停在下一步之前，顾问处理完等待的卡后自动继续（`on_card_resolved`），处理结果作为“顾问已确认事项”交给后续步骤，同一问题本次运行不再问。严格档（M4 ④b）读摘要卡 S-xx 起草、提示加 `STRICT_RULES`（标 `[S-xx]`、只用占位符），没有摘要卡发“证据不足”卡，输出不过占位符检查的本步失败、不保存；要写的 D3 / D4 已签署 422，同一项目同时只能有一个未结束的运行（409）；取消和 worker 改运行状态都先锁行再判断，模型返回后、写入前再看一次状态，已取消就不写；最后一步的卡处理完直接结束。接口 `/api/projects/{code}/runs`。审计只记编号、步骤、字数和用量。

保留期约定（M4 ①）：项目验收与保留期只经 `app/project/retention.py`（接口 `POST /api/projects/{code}/acceptance`，项目经理）：登记验收日（不晚于今天）和保留月数（0～24，默认 6，按合同约定或客户制度调整），平台算到期日（月末顺延到当月最后一天），项目列表和详情带 `retention_state`（active / retained / due_soon 到期前 30 天 / due）；可更正，每次进审计（只记日期和月数）。运维用 `python -m app.cli retention-report` 看到期情况。更正保留期后不再到期的，待确认的到期删除申请自动撤销。

摘要卡约定（M4 ④）：严格档的客户同意记录和去标识摘要卡只经 `app/project/cards.py`（接口 `/api/projects/{code}/consents`、`/api/projects/{code}/summary-cards`）。项目经理登记客户书面同意（文号 + 签署日期，不写客户名称，同一项目同时只有一条有效）；没有有效同意不能上传；项目成员整批上传摘要卡（评估项、现状、数量级、现场证据编号、客户审阅日期），先过机密级检测再过摘要卡规则（机构名、人名、联系方式、网络标识、系统 / 产品名和版本号、引号原话），任何一张不过整批拒收；内容相同不重复；编号 S-xx 不复用。上传者或项目经理可撤下单张卡、客户书面撤回同意时项目经理登记撤回，内容都立即清空。Agent 经 `project.evidence` 读摘要卡（严格档返回 S-xx，不新增工具授权）。项目删除时一并删除。审计只记编号、张数、字数和命中类别，不记卡片内容和文号。

删除约定（M4 ②）：项目资料删除只经 `app/project/deletion.py`（决策记录 2026-11-13）。到期由 worker 每小时扫描自动发起（`requested_by=platform`，也可 `python -m app.cli deletion-scan`）；客户书面要求提前删除由项目经理发起（`POST /api/projects/{code}/deletions`，写文号不写客户名，10 个工作日内完成，周一至周五计，不含节假日）；都由项目负责人确认（`/api/deletions`，不必是成员，不能是发起人），提前删除可由负责人写理由撤销，到期申请不能撤销。确认时必选经验回流情况（已回流填投料编号 / 无可回流 / 客户不同意回流），项目经理可标记“客户不同意回流”（`PUT /api/projects/{code}/flowback`），标记后只能选客户不同意；回流不阻断删除。确认后后台任务 `project.delete` 删对象存储 `{code}/` 下全部文件和证据、资料、交付件（含已签署）及章节版本、评审、签署、确认卡、对话、运行和成员关系，项目行保留（代号不复用、所有项目接口 404）；完成后的申请就是可交给客户的删除记录（只有编号、日期、计数、确认人和回流情况）。审计只记编号、日期和计数，不记文号和内容。运维 `python -m app.cli deletion-report [--all]`。

审计约定：审计只经 `app/core/audit.record()` 写入（只追加的哈希链，写入串行化）；不得 UPDATE / DELETE `audit_logs`（数据库触发器会拒绝）。以后的迁移如果要在 SQLite 上用 batch 模式改 `audit_logs`，要重建触发器。

登录约定：默认所有接口都要登录，公开接口只有 `/api/health` 和 `/api/auth/login`（白名单在 `app/core/auth.py`）；新增接口必须加进 `tests/test_auth.py` 的 `PROTECTED` 列表，否则测试失败。修改类请求（POST / PUT / PATCH / DELETE）要带请求头 `X-CSRF-Token`，值取登录接口或 `GET /api/auth/me` 返回体里的 `csrf_token` 字段（**不在 cookie 里**；会话 cookie `cp_session` 是 HttpOnly，脚本读不到）；改口令成功后会换新会话，要用新返回的 `csrf_token`。管理员在网页上建的账号或重置过口令的账号（`must_change_password`），改口令前只能访问 `PASSWORD_CHANGE_PATHS`（同一文件）。

权限约定（规则全文在 `app/core/permissions.py` 文件头）：业务接口在路由上挂 `require_business`；按项目取数据一律用 `project_for_member`（非成员 404）；系统管理员、安全审计员与其他角色互斥且看不到业务数据；审计员只能用命令行授予。新增或改动接口权限，必须同步更新 `tests/test_permissions.py` 的权限矩阵。

## 分支、提交与 PR

- 每个里程碑一个分支，例如 `feat/m1-auth`；一个 PR 只做一件事，便于审阅和回退。
- 提交信息用中文，以里程碑开头，例如 `M1：登录与会话`。
- PR 描述必须写：改了什么、怎么测（测试助手照着执行）、对应开发计划哪一项、是否涉及数据库迁移或权限变更。
- 合并条件：CI 全绿 → 测试助手在测试机上测试通过并在 PR 留评论 → 负责人合并。
- 开发与测试经 PR 通信，标签 `待测试` / `测试未通过` / `测试通过` 表示轮到谁（`docs/测试交接规范.md` 第 0 节）。开发助手开 PR 或修完推送后打 `待测试`；修复后的复测项写成标题为“复核说明”的 PR 评论。
- 开发助手可能从不同入口启动（桌面 app、飞书 cc-connect 等），但它们共用开发电脑上同一个仓库目录：**同一时间只让一个会话改仓库**（切分支、改文件、提交）。开工前 `git status`，发现不是自己改的未提交文件，先停下问负责人，不要覆盖、不要提交。
- 每个里程碑验收后打标签 `v0.1`、`v0.2`……，一期可用版本为 `v1.0`。

## 遇到以下情况先停下来问负责人

- 需要改动上面的数据保护红线或资质边界
- 需要引入新的外部服务或联网依赖
- 需要删除数据、重写迁移历史、强制推送
- 开发计划里标为“待决定”的事项（SSO 方式、模型接口、对象存储、Word 模板、现场工具形态）
