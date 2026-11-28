# Agent 收敛对接说明 · 第三步：报告与客户沟通（三期 C6③）

第一、二步写在 `docs/Agent收敛对接说明.md`；第三步单独成文，之后并入总说明。

md：`agents/roles/report-writer.md`（0.6 → 0.7）、`agents/roles/qa-reviewer.md`（0.1 → 0.2）。

| 代号 | 名称 | 合并自 | 模型档 |
|---|---|---|---|
| `report-writer` | 报告与客户沟通 | 报告与汇报 0.6 + `qa-reviewer` 0.1 的模式二（异议应答） | 标准（不变） |
| `qa-reviewer` | 质量评审（只做评审） | 原质量评审与异议应答，去掉模式二 | 不变 |

代号沿用 `report-writer`：main 的名册 `backend/app/agents/roster.py`（C6-P）已把“报告与客户沟通”登记为 `report-writer`，不另起 `report-comms`，所以不需要登记新 Agent、不需要旧名映射、不改授权（2026-11-27 修订，原草稿的 `report-comms` 作废）。

## 职责划分

- 模式 A 报告与汇报：原 report-writer 0.6 正文原样保留（产品段、写作规则、发现卡片、D4 现状评估报告起草时点、资质边界固定说法不变）。
- 模式 B 异议应答：原 qa-reviewer 模式二的输入、五段结构、口头版与书面版、角色模拟要点、示例搬入；新增衔接：应答口径与已交付报告一致，异议指向报告结论的单列“报告是否修改”（改哪一章哪一节、改稿后重新提交质量评审）；测评、认证追问写固定说法“等级测评由有资质的测评机构依程序开展”，不复述“保证 / 确保 / 一定能过”；新异议只标“新异议，建议入库”，经结项回流单入库（交付侧不写知识库）。
- 工具不变：`知识检索`、`读项目区`、`文档生成`（依据库引用走知识检索），不改授权矩阵。
- 质量评审 0.2：只做评审；挑战预判（TOP 5）保留；被要求组织异议应答时转交报告与客户沟通。`handoff_to` 为 `consult-coordinator`、`report-writer`。本步不改阻断定义和评审维度。

## 生效

两份 md 都改原文件，合并后热加载即生效（`agent_md_versions` 自动留存旧版，可回滚），同一次合并生效，中间没有空档。平台侧无改动。

## 评测

评测集 `knowledge/eval/agents/rcomm.yaml`（9 题：4 红线、4 要点、1 评审）。报告题和异议应答题都发给 `report-writer`；RCOMM-M01 发给 `qa-reviewer`，验证拆出异议应答后评审不退化。

```bash
# 测试机，真模型，每题 3 轮多数判定
for n in 1 2 3; do docker compose exec -T backend python -m app.cli agent-eval /srv/knowledge/eval/agents/rcomm.yaml --as <用户> --product SOC --out /tmp/rcomm-r$n.json; done
docker compose exec -T backend python -m app.cli agent-eval-majority /tmp/rcomm-r1.json /tmp/rcomm-r2.json /tmp/rcomm-r3.json
```

判定：9 题 3 轮多数通过，红线题一轮失败即不通过；SOC、IRE、DSM、ISO、NSG 产品评测集里 `report-writer` / `qa-reviewer` 的题不退化。

11-24 首测失败原因：评测集按旧代号把异议应答题（R03、G03、G04）发给已去掉异议模式的 qa-reviewer 0.2，属评测路由问题；本版改为发给 report-writer，并在 md 里补了 R03（固定说法）、G04（报告是否修改）的明确写法。
