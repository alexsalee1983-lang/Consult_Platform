# M4 端到端验收报告（acc68）

- 分支：`docs/m4-acceptance`，head `d11b316`
- 环境：`docker compose`（backend/frontend/worker/mock-llm/embed/rerank/postgres/redis/minio/capture-llm），后端 `127.0.0.1:18000`，前端 `127.0.0.1:18080`，现场工具 `127.0.0.1:18090/field-tool.html`
- 迁移：`0027_consent_summary_cards (head)`
- 验收日期：2026-11-13 ~ 2026-11-14
- 操作方式：只用页面、接口、`app.cli`；未直接改数据库；只用虚拟代号

## 一、总结论

| 节 | 内容 | 结论 |
|---|---|---|
| 1.1 | 客户同意（严格档） | 通过 |
| 1.2 | 现场工具（离线、占位符、加密工作文件、上传包） | 通过 |
| 1.3 | 摘要卡上传与查重 | 通过 |
| 1.4 | 起草（运行、占位符、人工拦截、评审、签署、导出） | **通过**（导出脚注缺陷已由 #69 修复并复核；评审链路环境受限未覆盖） |
| 1.5 | 回填正式稿 | 未执行（依赖 1.4 D4 已签署，受评审环境阻塞） |
| 1.6 | 撤回同意（GRID-F） | 通过 |
| 2 | 到期提醒（MFG-E） | 通过 |
| 3 | 删除（MFG-D 到期 / MFG-E 提前） | 通过 |
| 4 | 留痕与日志 | 通过 |

**唯一缺陷（已修复）**：导出 Word 时，摘要卡来源 `[S-01]`～`[S-05]` 不生成脚注。`backend/app/project/export.py` 的 `_REF` 正则只匹配 `[I-xx]`/`[D-xx]`/`[O-xx]`，不含 `[S-xx]`。严格档的核心来源就是摘要卡，导出后来源标注全部丢失，违反 M4「来源可追溯、有脚注」要求。

已开 issue #70，由 PR #69（`fix/m4-export-s-footnote`）修复，本报告出具后已按「由 #69 修复复核」重新验证通过（见 1.4 第 6 条第 2 段）。

**环境限制（非缺陷）**：质量评审 worker 依赖 mock-llm 返回评审 JSON，本地 mock 返回通用文本，评审以「评审输出格式不对」失败（review 50）。同一原因导致 1.4 签署链路、1.5 回填无法走到最终态。门禁本身行为正确（见 1.4）。

## 二、逐节记录

### 1.1 客户同意（GRID-E，strict，项目 id 22）
- `tester` 无同意上传摘要卡 → **422**；`tester` 登记同意 → **403**（仅项目经理）
- `qa-pm2` 登记同意 → **201**，`consent id=12`、文号 `同意-E01`、`signed_on=2026-11-13`、`recorder_by=qa-pm2`、`revoked=false`
- 模板 `consent_active:true`、`consent_id:12`

### 1.2 现场工具
- 断网可用：CSP `connect-src 'none'`，无外部请求
- 导入模板自动填充项目代号 `GRID-E`；对照表 4 行正确（`【客户】`/`■江北示范供电公司`/机构 等）
- 摘要卡页面已有 5 张；字段使用 `data-i` + `data-f`
- 自检：修正占位符后「5 张全部通过自检」；未改前如实标红
- 加密工作文件：`cp-card-work`，PBKDF2-SHA256 / AES-GCM，刷新后可恢复
- 上传包结构：`format=cp-field-package`、`version=1`、`kind=upload`、`kdf=PBKDF2/SHA-256/600000`、`hasPlainName=false`、`hasMapKey=false`，5 张卡、含占位符，**不含对照表**、不含真名
- 页面 CSP 阻断 fetch（设计如此）；导出包通过页面内 blob 抓取（浏览器下载未落盘）

### 1.3 摘要卡上传与查重
- 上传 → **201**，生成 S-01～S-05（`consent_id=12`）
- 完全相同的 `item+finding` 再传 → **409**，`duplicates:[{"card":1,"same_as":"S-01"}]`（查重键 `sha256(item+finding)`）
- 只改 finding 的构造测试返回 201（生成 S-06），非缺陷；S-06 已 withdraw（200，`withdrawn=true`），在线卡回到 S-01～S-05
- 泄漏抽检：`cards_leak=0`、`audit_leak=0`、`cards_live=19`、`audit_cardactions=63`
- 审计：5717 上传（5 张、chars=252、consent=12）、5718 上传 1 张 S-06、5719 withdraw S-06

### 1.4 起草
1. **运行按档位记 chain**：`POST /api/projects/GRID-E/runs` → **202**，run id=25，`chain=strict` ✅
2. run 25 **failed**：第 3 步 `整理事实清单` 写入 422；审计 5732 `deliverable.write blocked, deliverable=D3, reason=placeholder, categories=["机构名称"], section=1` —— 严格档设计预期（卡片正文含机构名称），审计可追、未落库 ✅
3. **人工写入拦截**：D4 写「由李处长负责 [S-01] 的特权账号整改。」→ **422**，`kinds:["人员姓名"]`，`savedLen=0` ✅
4. **占位符写入**：D4 第 1 章纯占位符 + `[S-01]`～`[S-05]` → **201**，`sources.counts.evidence=5`、`unresolved=[]`、`found=true`×5 ✅
5. **全文泄漏核查**：GRID-E `section_versions` 含「江北/天枢/玄武/李处长/QRadar」= **0**，共 138 字 ✅；`audit_logs` 全表同样为 0 ✅
6. **导出 D4 Word**（38762 字节）：
   - 正文为占位符版，`【客户】`/`【系统A】` 存在，无真名/占位符残留/IP/人名/产品名 ✅
   - `word/footnotes.xml` **只有分隔符，无任何来源脚注** ❌ **缺陷**（已由 #69 修复，见下段）
   - 补充验证（第 2 章写入 `[S-01]` + `[O-01]` + `[I-01]` 后重新导出，38993 字节）：`[O-01]`、`[I-01]` **生成脚注**（内容「未核对到对应的证据或知识库条目」，因该两编号在 GRID-E 不存在）；`[S-01]`～`[S-05]` **完全不生成脚注**。确认缺陷仅限 `[S-xx]`
   - **#69 修复复核（2026-11-14）**：切至 `fix/m4-export-s-footnote`（head `7de3673`）重建 backend，同一 GRID-E D4 重新导出 → 39211 字节，`word/footnotes.xml` 生成 8 条脚注节点：`S-01`～`S-05` 各一条（文案「去标识摘要卡《…》，客户审阅 2026-11-13」），第 2 章重复引用 `[S-01]` 再生成一条，`[O-01]`/`[I-01]` 照旧。正文 8 处引用 ↔ 8 个 `w:footnoteReference` ↔ 8 条脚注一一对应。已撤回的 `S-06` 不生成脚注（`withdrawn` 过滤生效）。→ **1.4 第 4 条「来源有脚注」由 #69 修复复核通过** ✅
7. **质量评审**：`POST .../reviews` → **202**，review id=50（`queued`）；worker 处理时 `embed`/`rerank` 502、mock-llm 返回通用文本，最终 `status=failed`、`error=评审输出格式不对` —— 本地环境限制，非 PR 缺陷
8. **签署门禁**（行为正确）：
   - `tester` 提交签署 → **409** `{gate:false, reasons:["还没有完成的质量评审"]}`
   - `tester` 签署 → **403**「只有项目经理能签署」
   - `qa-pm2` 提交 → **409** 同上；直接签署 → **409**「D4 还没有提交签署」（顺序约束正确）

### 1.5 回填正式稿
未执行：需 D4 已签署后再用现场工具「占位符回填」载入 Word 生成正式稿；被 1.4 评审环境阻塞。

### 1.6 撤回同意（GRID-F，strict，`create-project --code GRID-F --tier strict --manager qa-pm2`）
- 登记同意 → **201**，`consent id=13`、文号 `同意-F01`
- 上传 2 张卡：首次 `onsite_refs=["现场-1"]` → **422** `证据编号格式`（逐张指出）；改 `O-01`/`O-02` → **201**，S-01、S-02（`consent_id=13`）
- D3 大纲 + 正文 `[S-01]`/`[S-02]` → **201**，45 字
- 撤回：`POST /consents/13/revoke` `{revoke_ref:"撤回-F01", received_on:"2026-11-13"}` → **200**，`revoked=true`
- 撤回后状态：活跃同意 **0**、在线摘要卡 **0**、S-01/S-02 `chars=0`/`withdrawn=true`、D3 正文 `len=0`
- 撤回后写入 → **422**「严格档项目没有有效的客户书面同意…」；发起交付链 → **422**
- 审计 5776 `project.consent.revoke {"consent":13,"cards_cleared":2,"versions_cleared":1,"reviews_cleared":0,"runs_cancelled":0}` —— 计数与实际一致，**无文号内容** ✅
- 版本行保留（id 222，`char_count=0`、`body` 空）✅；审计 5777 `deliverable.write blocked reason=no_consent`、5778 `run.start denied reason=no_consent` ✅

### 2 到期提醒（MFG-E，standard）
- 项目经理登记验收：`accepted_on=2026-06-04`、`retention_months=6` → **200**，`deletion_due_on=2026-12-04`、`retention_state=due_soon`（剩余 20 天 ≤ `REMIND_DAYS=30`）✅
- 月末顺延：`add_months` 验证 `2026-08-31 + 6 → 2027-02-28`、`2026-05-31 + 6 → 2026-11-30`、`2026-06-30 + 6 → 2026-12-30` ✅
- `retention-report` 列出 MFG-E：验收 2026-06-04、保留 6 个月、到期 2026-12-04、剩余 21 天、即将到期 ✅
- `deletion-scan` → 「发起到期删除申请 0 个」（未到期，不发起）✅

### 3 删除
#### 3.1 准备数据（MFG-D，standard）
- 上传资料 → **201** `D-01`（`生产车间现状说明`）
- 访谈纪要 → **201** `I-01`；现场观察 → **201** `O-01`
- D4 大纲 + 一章 → **200** / **201**（61 字，引用 `[D-01]`/`[I-01]`/`[O-01]`）

#### 3.2 仅现场使用（D-01）
- `POST .../evidences/D-01/local-only {request_ref:"现场-D01"}` → **200**，`local_only=true`
- 详情：`filename=""`、`content_len=0`、`preview=0`，**无文件名与预览** ✅
- 同文件重传 → **422**「这份资料已标为仅现场使用（证据编号 D-01），不能上传」✅
- 正文引用 `[D-01]` → **422**「D-01 已标为仅现场使用，不能在交付件里引用」✅

#### 3.3 到期删除
- 更正验收为 `accepted_on=2026-05-01` / 6 个月 → **200**，`due=2026-11-01`、`state=due`
- `deletion-scan` → 「发起到期删除申请 1 个：MFG-D」✅，删除申请 id=8（`kind=expiry`、`requested_by=platform`）
- 负面检查：
  - `qa-pm2`（项目经理）`confirm` 未选回流 → **422** `flowback` 必填；`cancel` → **403**「只有项目负责人能确认或撤销删除」✅
  - 项目负责人 `lead68` `cancel` → **409**「到期删除不能撤销；保留期另有约定的，请项目经理更正保留期」✅（到期申请无撤销入口）
- `lead68` 确认删除 `{flowback:"none"}` → **200**，`status=confirmed`、`confirmed_by=lead68` ✅

#### 3.4 删除记录抽检
- `deletion-report --all`：#8 MFG-D → 已删除、应完成 2026-11-01、确认 lead68、完成 2026-11-13、回流「无可回流内容」✅
- 记录字段：`kind=expiry`、`status=done`、`requested_by=platform`、`confirmed_by=lead68`、`flowback=none`、`completed_at`、`counts`、`job_id=294`
- `counts` 与 3.1 实际一致：`documents=1`、`evidences=3`、`section_versions=1`、`deliverables=8`、`members=3`、`files=1`；**无正文、无文件名、无文号** ✅
- 数据清除：MFG-D `evidences=0`、`section_versions=0` ✅
- 项目页/接口 → **404**「项目不存在」，项目列表不含 MFG-D ✅
- 重建 `create-project --code MFG-D` → 拒绝「项目 MFG-D 已存在」✅
- 对象存储 `/data/project-area` 中 `MFG-D` 匹配数 = **0** ✅

#### 3.5 提前删除（MFG-E）
- 项目经理发起 `{request_ref:"删除-E01", received_on:"2026-11-13"}` → **201**，id=9、`kind=early`、`due_by=2026-11-27`（10 个工作日后）、`pending` ✅
- 项目经理尝试撤销 → **403**（规则：只有项目负责人；且不能撤销自己发起的）✅
- 项目负责人 `lead68` 撤销 `{reason:"客户暂不删除，继续保留至到期"}` → **200**，`status=cancelled`、`cancelled_by=lead68`、理由已记录 ✅

### 4 留痕与日志
- `verify-audit`：**审计链完整**，链上 5787 条，链前旧记录 20 条，链头 `id=5807`
- 后端 + worker 日志搜「江北 / 天枢 / 李处长」→ **0**
- `audit_logs` 全表搜「江北 / 天枢 / 李处长 / QRadar」→ **0**

## 三、缺陷清单

### 缺陷 1（已修复，由 #69 复核通过）：导出 Word 时摘要卡来源 `[S-xx]` 不生成脚注
- 位置：`backend/app/project/export.py:57`
- 现状：`_REF = re.compile(r"\[((?:I|D|O)-\d{2,4})(-\d{1,3})?\]|...")`，缺 `S`
- 影响：严格档交付件的核心来源是摘要卡；导出后脚注全部缺失，读者无法从正文追溯到卡
- 复现：GRID-E D4 正文写 `【系统A】…（见 [S-01]）` → 导出 docx → `word/footnotes.xml` 仅分隔符
- 对照：同文档写 `[O-01]`/`[I-01]` 会生成脚注（证明仅 `S` 缺失）
- 修复：PR #69 `fix/m4-export-s-footnote`（`_REF` 补 `S`，并为摘要卡来源补脚注文案，按 `withdrawn` 过滤）
- 复核：同一交付件重新导出，`[S-01]`～`[S-05]` 均生成脚注，8 处引用 ↔ 8 条脚注对应，泄漏抽检 0 命中 → **通过**

## 四、环境限制（非缺陷）
1. worker 调 `embed:8000`/`rerank:8000` 持续 502（容器内部服务未就绪），但 `embed`/`rerank` 自身启动正常
2. `mock-llm` 对 `/v1/chat/completions` 返回通用文本，无评审 JSON 剧本 → 质量评审必然 `error=评审输出格式不对`
3. 结果：1.4 第 7～8 条无法走到「评审通过→提交→签署」的最终态；1.5 依赖前者，未执行
4. 评审/签署门禁本身的负面行为已验证通过（409/403 均正确）

## 五、账号与凭据说明
- `tester`：从 `.env` 的 `TEST_ACCOUNT_PASSWORD` / `TEST_ACCOUNT_TOTP_SECRET` 生成（`t_creds.txt`）
- `qa-pm2`：密码取自本地 `creds.txt`；TOTP 经 `reset-totp` 重置后写入 `pm2_creds.txt`
- `lead68`：为 3.3 确认角色经 `app.cli create-user` 新建并 `grant-role project_lead`（`done_creds.txt`）
- 所有动态码/密钥/口令**仅在本地文件**，未进聊天、PR 评论、报告
