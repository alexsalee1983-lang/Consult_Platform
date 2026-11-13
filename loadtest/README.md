# 压测（M5 ⑥）

方案、通过标准和步骤见 `docs/压测方案.md`。只在测试机本地运行，只用虚拟项目和压测账号。

- `locustfile.py`：场景与阈值判定（未达标退出码 1）
- `requirements.txt`：Locust，装在独立虚拟环境里
- `accounts.json`：`python -m app.cli loadtest-accounts` 生成的凭据，已在 `.gitignore`，压测完删除
