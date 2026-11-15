# 公开资料原文（内容线 · 开发线 2）

按批次存放从官方网站下载的**法律、行政法规**原文和下载清单（`manifest.csv`，列见 `backend/app/kb/sources.py`）。
法律、行政法规不受著作权保护，原文可入库；**国家标准、行业标准不放原文**，只收条款号、标题、解读和映射（决策记录）。

| 批次 | 内容 | 份数 / 条数 | 来源 |
|---|---|---|---|
| batch1 | 网络安全、数据、个人信息、密码、关基、保密、反间谍、反诈法律法规（现行版） | 10 份 / 622 条 | 国家法律法规数据库 flk.npc.gov.cn |

文号已对照官方公布逐份核对；`sha256` 为下载原件哈希。导入（测试机后端容器内）：

```bash
python -m app.cli kb-source-split /srv/knowledge/sources/batch1/manifest.csv --out /tmp/batch1
python -m app.cli kb-source-publish /tmp/batch1 --as <知识专家>   # 平台核验 + 5% 抽检（B10，决策记录 2026-11-15）
```

国家安全法、电子签名法文号待核实，下一批补入。
