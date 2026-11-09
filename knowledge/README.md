# 知识条目文件

- `samples/`：**示范条目**，只用于在测试机上跑通“导入 → 双人审核 → 发布 → 检索 → 评测”全流程。
  内容是开发侧按业界通行做法写的示例，**不是专家审定的正式知识**；依据类条目须由专家对照现行有效文本
  （含修正）核对后才能用于正式环境。正式的约 300 条由知识库建设 Agent 从公开资料起草、专家评审后入库
  （决策记录 2026-11-09）。
- `eval/`：评测题。`samples.yaml` 对应示范条目；正式评测题在正式条目起草后编写，经知识专家评审。

格式见 `backend/app/kb/files.py` 文件头。常用命令（在后端容器里）：

```bash
python -m app.cli kb-import /srv/knowledge/samples --as <知识专家> [--submit]
python -m app.cli kb-draft --source <公开资料文件> --source-detail "<来源说明>" --out /tmp/drafts --as <知识专家>
python -m app.cli kb-eval /srv/knowledge/eval/samples.yaml --as <用户> [--out report.json]
```
