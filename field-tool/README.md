# 严格档现场工具（离线）

单个网页文件 `field-tool.html`，在公司笔记本上用浏览器直接打开（Edge / Chrome 较新版本），不联网、不连模型、不装任何东西。依据：决策记录 2026-11-10（现场工具形态）、2026-11-13（离线优先）；《客户数据告知模板与合同条款》严格档条款。

## 流程

1. **离场前（公司网络）**：在平台登记客户书面同意后，下载本项目的空白模板 `GET /api/projects/{代号}/summary-cards/template`，存为 `.json`，和本页一起拷到笔记本。
2. **现场**：
   - 导入模板（自检规则以模板里的 `checks` 为准；不导入则用页面内置的一份）。模板里的 `items` 是本项目服务产品规定的评估项清单（二期 A8），非空时填卡给候选值、不在清单的自检报“评估项不在清单”，平台上传同样拒收；为空不限。
   - 对照表：占位符（如【客户】【系统A】【人员1】）↔ 真实名称。只用于本机自检和回填，**不进上传包**。
   - 填摘要卡：评估项、现状、数量级、现场证据编号（D-03、I-04-07 等，原文留现场）、客户对接人审阅签字日期。页面按平台同一套规则实时自检，另查对照表里的真名。
   - 随时“保存加密工作文件”（`.cpwork`，含对照表，只留本机加密目录）。
   - 客户对接人逐份审阅签字后，导出上传包（`.cpcard`，只含摘要卡）。
3. **离场后（公司网络或 VPN）**：在平台上传摘要卡（⑥ 的上传页会直接解密 `.cpcard`；之前可在本页“查看加密包”解密，正文即 `POST /api/projects/{代号}/summary-cards` 的请求体）。口令另行告知上传人，不随文件传递，不写进聊天。
4. **回到现场**：把平台导出的占位符版本 Word 草稿带去，在“占位符回填”按对照表生成正式稿。正式稿含真实信息，只在客户现场移交。占位符被 Word 格式拆开时替换不到，页面会列出，手工处理。

## 加密包格式

```json
{"format": "cp-field-package", "version": 1, "kind": "upload" | "work",
 "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": 600000, "salt": "<base64 16 字节>"},
 "cipher": {"name": "AES-GCM", "iv": "<base64 12 字节>"},
 "data": "<base64 密文>"}
```

- 密钥：PBKDF2-SHA256(口令, salt, iterations) → AES-256-GCM；附加数据（AAD）为 `cp-field-package:1:<kind>`，改了 kind 解不开。口令至少 12 位。
- `upload` 明文：`{"project_code": "...", "cards": [{"item", "finding", "magnitude", "onsite_refs": [...], "client_reviewed_on"}]}`。
- `work` 明文：`{"project_code", "template", "mapping": [{"placeholder", "real", "kind"}], "cards"}`。

## 维护

- 规则来自后端 `cards.field_checks()`。改了 `backend/app/project/cards.py` 或 `backend/app/kb/deid.py` 的规则，在 backend 目录运行 `python ../scripts/update_field_tool_checks.py`，并按需在 `test/cases.json` 加用例（平台和页面各自核对同一份期望）。
- 测试：`node --test field-tool/test/core.test.mjs`（Node 20 以上，无依赖）；`backend/tests/test_field_tool.py`。
- 页面只用 `frontend/src/theme.css` 的颜色令牌，不加任何外部资源。机密级只做简化检查，以平台上传时的检查为准。
