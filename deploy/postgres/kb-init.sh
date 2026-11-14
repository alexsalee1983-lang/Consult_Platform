#!/bin/sh
# 知识区数据库和账号（M5 ③ 分库，决策记录 2026-11-09）。可重复执行。
#   新数据卷：postgres 容器首次初始化时自动执行（/docker-entrypoint-initdb.d）
#   已有数据卷、恢复备份后：docker compose exec postgres kb-init.sh
# 建账号 KB_POSTGRES_USER（默认 consult_kb，非超级用户、不能建库建角色）和它拥有的库 KB_POSTGRES_DB（默认 consult_kb），
# 在库里装 pgvector、zhparser 和检索配置 zh（知识区账号装不了扩展）；两个库都收回 PUBLIC 的连接权限，
# 知识区账号连不上主库。口令只从环境变量 KB_POSTGRES_PASSWORD 读，不出现在命令行参数里。
set -eu
: "${KB_POSTGRES_PASSWORD:?要设置 KB_POSTGRES_PASSWORD（.env）}"
export KB_USER="${KB_POSTGRES_USER:-consult_kb}" KB_DB="${KB_POSTGRES_DB:-consult_kb}"
MAIN_DB="${POSTGRES_DB:-consult}"
SU="${POSTGRES_USER:-consult}"
[ "$KB_DB" != "$MAIN_DB" ] && [ "$KB_USER" != "$SU" ] || { echo "知识区的库名和账号都要与主库不同" >&2; exit 2; }

psql -v ON_ERROR_STOP=1 -q -U "$SU" -d "$MAIN_DB" <<'SQL'
\getenv kbuser KB_USER
\getenv kbdb KB_DB
\getenv kbpw KB_POSTGRES_PASSWORD
SELECT format('CREATE ROLE %I LOGIN', :'kbuser') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'kbuser') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'kbuser', :'kbpw') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'kbdb', :'kbuser') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'kbdb') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'kbdb') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', :'kbdb', :'kbuser') \gexec
SQL

psql -v ON_ERROR_STOP=1 -q -U "$SU" -d "$KB_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS zhparser;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'zh') THEN
    CREATE TEXT SEARCH CONFIGURATION zh (PARSER = zhparser);
    ALTER TEXT SEARCH CONFIGURATION zh ADD MAPPING FOR n,v,a,i,e,l,j,x WITH simple;  -- 同主库迁移 0009
  END IF;
END $$;
SQL
echo "知识区数据库 $KB_DB、账号 $KB_USER 已就绪"
