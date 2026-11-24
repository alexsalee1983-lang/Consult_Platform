# Agent 收敛对接说明 · 第四步：知识建设与萃取（三期 C6④）

第一、二步写在 `docs/Agent收敛对接说明.md`，第三步在 `docs/Agent收敛对接说明-第三步.md`（#411）；为免几个未合并 PR 改同一文件，第四步单独成文，都合并后并入总说明。

md 草稿：`agents/roles/kb-builder.md` 0.5.1（#334）→ 0.6.0。本 PR 叠在 #334 分支上（kb-builder md 同一 PR 序列），#334 合并后再合本 PR。

| 代号 | 名称 | 合并内容 | 模型档 |
|---|---|---|---|
| `kb-builder`（不变） | 知识建设与萃取 | 知识建设（A 盘点、B 加工、E 保鲜、F 知识源清单、I 提炼与挂载）+ 经验萃取（C 工作坊、G 反向提炼、H 缺槽访谈、D 结项沉淀） | 不变 |

## 改了什么

- display_name “知识库建设 Agent” → “知识建设与萃取 Agent”；代号、工具、知识区读写范围不变（写只到待入库区）。
- 新增“模式总览”：九个模式按建设 / 萃取分类，写明什么时候用、产出、去向；回复第一行写明所用模式，一次只走一个模式。合成五期才开。
- 新增“萃取的共同规则”：只由事件触发（工作坊、历史交付物导入、缺槽、结项回流单；四期起平台触发引擎按信号下发）；一律是候选、带出处；第三级审核（知识管理员确认去标识，一位非作者知识专家拍板）；不碰项目区。
- 模式 D 结项沉淀输入改为平台生成的结项回流单和已去标识放进待入库区的交付物，不读项目原件（对齐红线 6 和第三步 report-comms “新异议，建议入库”经结项回流单入库）。
- 不适用与交接：起草对客制度改指方案与制度设计 Agent，异议应答改指报告与客户沟通 Agent，审核交知识审核 Agent；`handoff_to` 由 `[sanitizer]` 改为 `[sanitizer, kb-reviewer]`（sanitizer 三期保留，决策记录 2026-11-23）。
- 各模式正文（A～I）不改。

## 平台侧对接（C6-P，不在本 PR）

1. `agents` 表 kb-builder 的显示名同步为“知识建设与萃取”（代号不变，不用旧名兼容）。
2. 授权不变：`kb.search`、`basis.lookup`、`kb.stage_write`、`deid.check`；权限矩阵测试不用改。
3. 结项回流单：模式 D 依赖平台把交付侧标“新异议，建议入库”的内容、踩坑、模板改进去标识后放进待入库区；未接入前，模式 D 只处理人工导出到待入库区的去标识材料。
4. md 改原文件，热加载即生效；`agent_md_versions` 自动留存 0.5.1，可回滚。

## 评测：前后对比的跑法

评测集 `knowledge/eval/agents/kbx.yaml`（9 题：4 红线、5 要点）。代号不变，比的是 md 版本。

```bash
# 合并前基线（测试机，真模型，攒批跑；kb-builder 此时是 0.5.1）
docker compose exec backend python -m app.cli agent-eval /srv/eval/agents/kbx.yaml --as <用户> --product SOC --out /tmp/kbx-before.json
# 本 PR 部署（kb-builder 0.6.0）后同一批题重跑；kb-builder 已有的评测集一并重跑
docker compose exec backend python -m app.cli agent-eval /srv/eval/agents/kbx.yaml --as <用户> --product SOC --out /tmp/kbx-after.json
docker compose exec backend python -m app.cli agent-eval /srv/eval/agents/extract.yaml --as <用户> --product SOC --out /tmp/extract-after.json
```

判定：每题 3 轮多数判定，基线通过的题合并后仍通过；kb-builder 原有模式 F、G、H、I 的评测集（`knowledge/eval/agents/` 下与资料仓里的）不退化。KBX-G05（回复第一行写明模式）和 KBX-R02（不读项目区、改用结项回流单）是本步新写明的规则，基线不通过不算退化。
