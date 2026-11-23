# Agent 收敛对接说明（三期 C6 / C6-P）

C6 把 Agent 收敛为 9 个，按步走、每步单独 PR、经负责人本人确认、评测回归不退化。本文件记每一步的新旧代号对应和平台侧（线 1 C6-P）要接的事，后续步骤在这里续写。

## 第一步：咨询协调（C6①，md 草稿见 `agents/roles/consult-coordinator.md`）

| 新代号 | 名称 | 合并自 | 模型档 |
|---|---|---|---|
| `consult-coordinator` | 咨询协调 | `consultant-lead`（咨询师 0.4）+ `delivery-pm`（项目交付管理 0.1） | 标准 |

### 职责划分

- 派活与转交：原咨询师的分派表、拆分顺序、【交接】格式不变；转交目标去掉项目交付管理（并入本 Agent）。
- 项目管理模式 A～E：原项目交付管理的启动（D1）、周报、阶段确认、范围变更、会议纪要，原样搬入。
- 新增模式 F 结项回流单预填（4A 架构设计 3.4）：只出草稿给项目经理，入库仍经平台去标识与知识审核；项目标记“客户不同意回流”时不预填。
- 分析与设计（D2～D8）不做，继续转交；本 Agent 不写知识库（交付侧硬边界不变）。

### 平台侧对接（C6-P，不在本 PR）

1. **登记**：`agents` 表新增 `consult-coordinator`（side=delivery，启用）；md 已写 `model_tier: 标准`。
2. **授权**：两个旧 Agent 授权的并集，不新增写入类工具：`kb.search`、`project.read`（含隐含的 `project.evidence`、`confirm.create`）、`kb.mapping`、`doc.generate`、`agent.handoff`。`agent.handoff` 只授权给主入口，迁移 0033 的约束改为对新代号成立；权限矩阵测试同步。
3. **旧名兼容**：
   - 立项时冻结 Agent 版本的进行中项目，继续按旧代号和旧 md 跑；旧 md 暂不删，`consultant-lead`、`delivery-pm` 保持可加载。
   - 新立项默认入口改为 `consult-coordinator`（前端 `Chat.jsx` 的 `DEFAULT_AGENT`）；新建对话、转交时收到旧代号的，按 `replaces` 映射到新代号（md frontmatter 新增 `replaces` 字段，注册表现在不读，C6-P 决定是否读它）。
   - 转交解析（`app/agents/handoff.py`）：其他 Agent 回复里写“目标 Agent：项目交付管理 / 咨询师”的，映射到咨询协调。
   - D1 步的负责 Agent 由项目交付管理改为咨询协调（交付链配置）。
4. **其他 Agent md 的 `handoff_to`** 里还写着 `consultant-lead`，随后续 C6 步骤逐个改，C6-P 期间靠旧名映射兜底。

### 评测：前后对比的跑法

评测集 `knowledge/eval/agents/coord.yaml`（11 题，虚拟客户 BANK-A、GRID-B、MFG-C）按旧代号写，作基线：

```bash
# 合并前基线（测试机，真模型，攒批与其他大评测一起跑）
docker compose exec backend python -m app.cli agent-eval /srv/eval/agents/coord.yaml --as <用户> --product SOC --out /tmp/coord-before.json
# C6-P 登记新 Agent 后：替换代号重跑同一批题
sed -E 's/agent: (consultant-lead|delivery-pm)/agent: consult-coordinator/' knowledge/eval/agents/coord.yaml > /tmp/coord-after.yaml
docker compose exec backend python -m app.cli agent-eval /tmp/coord-after.yaml --as <用户> --product SOC --out /tmp/coord-after.json
```

判定：基线通过的题，换新 Agent 后仍通过（每题 3 轮多数判定，C8 `agent-eval-majority`）；COORD-G06 回流单是新职责，基线不通过不算退化。SOC 等产品评测集里没有这两个旧 Agent 的题，不受影响。
