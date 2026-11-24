# Agent 收敛对接说明 · 第三步：报告与客户沟通（三期 C6③）

第一、二步写在 `docs/Agent收敛对接说明.md`（第二步在 #408 未合并，为免两个未合并 PR 改同一文件，第三步单独成文，两者都合并后并入总说明）。

md 草稿：`agents/roles/report-comms.md`（新）、`agents/roles/qa-reviewer.md`（0.1 → 0.2）。

| 新代号 | 名称 | 合并自 | 模型档 |
|---|---|---|---|
| `report-comms` | 报告与客户沟通 | `report-writer`（报告与汇报 0.5）+ `qa-reviewer` 0.1 的模式二（异议应答） | 标准（md 不写 model_tier，与 report-writer 一致） |
| `qa-reviewer` | 质量评审（只做评审） | 原质量评审与异议应答，去掉模式二 | 不变 |

## 职责划分

- 模式 A 报告与汇报：原 report-writer 正文原样搬入，产品段（SOC、IRE、DSM、ISO、NSG）逐字不变，写作规则、发现卡片、资质边界固定说法不变。
- 模式 B 异议应答：原 qa-reviewer 模式二的输入、五段结构、口头版与书面版、角色模拟要点、示例原样搬入；新增两条衔接：应答口径与已交付报告一致，要改报告结论的回到模式 A 改稿并重新提交质量评审；新异议只标“新异议，建议入库”，经结项回流单入库（交付侧不写知识库、不直接转交知识侧 Agent，原 qa-reviewer 的 `handoff_to: kb-builder` 去掉）。
- 质量评审 0.2：只做评审；“挑剔的客户”的挑战预判（TOP 5）保留在评审输出里，角色要点表改名“挑战预判的角色要点”；被要求组织异议应答时转交报告与客户沟通。`handoff_to` 改为 `consult-coordinator`、`report-comms`。
- 本步不改质量评审的阻断定义和评审维度（决策 ⑤ 的封闭五类另走 PR）。

## 平台侧对接（C6-P，不在本 PR）

1. **登记**：`agents` 表新增 `report-comms`（side=delivery，启用）。
2. **授权**：report-writer 的授权加 `basis.lookup`（异议应答引用依据，原 qa-reviewer 有），不新增写入类工具：`kb.search`、`basis.lookup`、`project.read`、`doc.generate`；qa-reviewer 授权不变。权限矩阵测试（`tests/test_tools.py`）同步。
3. **旧名兼容**：report-writer 旧 md 暂不删，进行中项目按立项冻结版本跑；转交收到 `report-writer` 的按 `replaces` 映射；转交解析里“目标 Agent：报告与汇报”映射到报告与客户沟通（“质量评审与异议应答”按前缀仍落到 qa-reviewer，需要时 C6-P 把含“异议应答”的转交改落到 report-comms）；交付链 D4、D7、D8 的负责 Agent 改指新代号。
4. **qa-reviewer 0.2 与 report-comms 登记同时生效**：qa-reviewer 改的是原文件，md 一改热加载即生效（版本库 `agent_md_versions` 自动留存 0.1，可回滚）；所以本 PR 应在 C6-P 登记 report-comms 之后、或同一窗口合并，否则中间这段没有 Agent 做异议应答。
5. **其他 Agent md 的 `handoff_to`** 里还写着 `report-writer`（compliance-basis、research-interview 等），随后续步骤逐个改，期间靠旧名映射兜底。

## 评测：前后对比的跑法

评测集 `knowledge/eval/agents/rcomm.yaml`（9 题：4 红线、4 要点、1 评审），按旧代号写作基线，每题 `after` 字段是合并后的代号。另外 SOC、IRE、DSM、ISO、NSG 产品评测集里 `agent: report-writer` 的题换代号重跑，`agent: qa-reviewer` 的评审题按 qa-reviewer 0.2 重跑。

```bash
# 合并前基线（测试机，真模型，攒批跑；qa-reviewer 此时是 0.1）
docker compose exec backend python -m app.cli agent-eval /srv/eval/agents/rcomm.yaml --as <用户> --product SOC --out /tmp/rcomm-before.json
# C6-P 登记 report-comms、qa-reviewer 换 0.2 后：agent 换成 after 重跑
python -c "import yaml,sys;c=yaml.safe_load(open(sys.argv[1],encoding='utf-8'));[d.update(agent=d['after']) for d in c];yaml.safe_dump(c,open(sys.argv[2],'w',encoding='utf-8'),allow_unicode=True,sort_keys=False)" knowledge/eval/agents/rcomm.yaml /tmp/rcomm-after.yaml
docker compose exec backend python -m app.cli agent-eval /tmp/rcomm-after.yaml --as <用户> --product SOC --out /tmp/rcomm-after.json
# 产品评测集：只替换 report-writer
sed -E 's/agent: report-writer/agent: report-comms/' knowledge/eval/agents/soc.yaml > /tmp/soc-after.yaml   # ire/dsm/iso/nsg 同法，按各自 --product
```

判定：基线通过的题，换新 Agent 后仍通过（每题 3 轮多数判定）；RCOMM-G04（异议要求改报告结论时回到改稿并重新评审）是合并后的新衔接，基线不通过不算退化；RCOMM-M01 与产品集里的 qa-reviewer 评审题验证拆出异议应答后评审不退化。
