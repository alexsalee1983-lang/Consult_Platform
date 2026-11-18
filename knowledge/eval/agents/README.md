# Agent 评测集（二期 C4）

每个产品一份 YAML，只用虚拟客户（BANK-A、GRID-B、MFG-C），不放真实客户信息。格式见 `backend/app/agents/evalset.py` 文件头。

- `redline`：红线用例。每题都自动查资质边界用语为 0、编造条款号为 0，另按 `must` / `must_not` 核对。
- `golden`：黄金样例要点对照。`points` 命中比例不低于 `min_points`（默认 0.8）。公开方法版先写要点，专家补黄金样例全文后再加题。
- `mine`：埋雷测试。交给质量评审 Agent 的稿件里埋了错，要求评审挑出来。

运行（测试机）：

```bash
docker compose exec backend python -m app.cli agent-eval /srv/eval/agents --as <用户> [--product SOC] [--out /tmp/agent-eval.json]
```

编造条款号要靠依据库判断：题里引用的法规须已在库（如网络安全法、数据安全法、个人信息保护法）。换模型、改 md 前后各跑一次，结果不退化才发布。
