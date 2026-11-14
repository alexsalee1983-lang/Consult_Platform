# 模块配置

每个模块一个 `<代号小写>.yaml`，格式见 `docs/接口约定.md` 第三节。模块只追加章节、框架、依据、步骤说明、门禁、摘要卡评估项和检索标签，不单独建交付链；`products` 与产品配置的 `modules` 两边都要写。

本地校验：`cd backend && python -m app.cli catalog-check`。
