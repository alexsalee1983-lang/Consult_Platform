# 服务产品配置

每个产品一个 `<代号小写>.yaml`，格式见 `docs/接口约定.md` 第二节。门禁规则用到的清单文件（只写条款号，不写标准全文）放 `rules/`。

- 开发线 2 起草、负责人审批后合并；改配置只影响之后立项的项目（立项即冻结）。
- 本地校验：`cd backend && python -m app.cli catalog-check`（CI 由 `tests/test_product_config.py` 把关）。
- 不写客户信息，配置里的文字都过机密级检测。
