# Agent 评测集（二期 C4）

每个产品一份 YAML，只用虚拟客户（BANK-A、GRID-B、MFG-C），不放真实客户信息。格式见 `backend/app/agents/evalset.py` 文件头。

- `redline`：红线用例。每题都自动查资质边界用语为 0、编造条款号为 0，另按 `must` / `must_not` 核对。
  资质边界只数引号外的：在“”「」里引用原话指出问题算提及（报告里记“引用原话 N 处（不计）”），不算使用。
- `golden`：黄金样例要点对照。`points` 每个要点是正则，可用 `|` 写同义备选，命中比例不低于 `min_points`（默认 0.8）。公开方法版先写要点，专家补黄金样例全文后再加题。
- `mine`：埋雷测试。交给质量评审 Agent 的稿件里埋了错，要求评审挑出来。

`extract.yaml`：知识侧 kb-builder 模式 I（提炼与挂载，三期 C12）的平台回归最小样例；正式内容题放资料仓 Consult_Knowledge。

运行（测试机）：

```bash
docker compose exec backend python -m app.cli agent-eval /srv/eval/agents --as <用户> [--product SOC] [--out /tmp/agent-eval.json] [--answers /tmp/agent-answers] [--case-timeout 300]
```

编造条款号要靠依据库判断：题里引用的法规须已在库（如网络安全法、数据安全法、个人信息保护法）。换模型、改 md 前后各跑一次，结果不退化才发布。

`--answers` 把每题答案原文写成 `<题号>.md`，只在开发、测试环境用于回放核对未通过的题（生产拒绝），不要提交或外传。

每题跑完立即打印一行进度（`[序号/总数] 题号 结论 耗时`），`--answers` 逐题落盘；单题超过 `--case-timeout` 秒（默认 300，0 不限）记“出错：超时”，继续下一题。
