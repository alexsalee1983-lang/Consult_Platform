# 数据库镜像

PostgreSQL 16 + pgvector + zhparser（中文分词）。`docker compose` 会自动构建，不需要单独操作。

| 组件 | 版本 | 用途 |
|---|---|---|
| 基础镜像 `pgvector/pgvector` | 0.8.6-pg16-bookworm | PostgreSQL 16 + 向量检索 |
| SCWS | 1.2.3 | 中文分词库（zhparser 依赖） |
| zhparser | 2.3 | PostgreSQL 中文分词扩展，全文检索配置 `zh`（迁移 `0009_zh_search` 创建） |

- 源码包都固定版本并校验 sha256。升级时同时改 `Dockerfile` 里的版本号和 sha256，并在 PR 里说明。
- 构建机连不上外网时，用构建参数指向公司内部镜像：在 `.env` 里设 `SCWS_URL`、`ZHPARSER_URL`。
- 数据卷与原来的 `pgvector/pgvector:pg16` 兼容（同为 PostgreSQL 16），已有数据不用迁移。

验证分词：

```bash
docker compose exec postgres psql -U consult -d consult -c "SELECT to_tsvector('zh', '安全运营中心的值班制度')"
```
