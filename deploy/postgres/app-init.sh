#!/bin/sh
# 主库应用账号（M5 ⑧，等保自查 ②：应用不用超级用户连库）。可重复执行。
#   新数据卷：postgres 容器首次初始化时自动执行（/docker-entrypoint-initdb.d）
#   已有数据卷（升级到 v1.0）、恢复备份后：docker compose exec postgres app-init.sh
# 建账号 APP_POSTGRES_USER（默认 consult_app，非超级用户、不能建库建角色），把主库和库里 public 下
# 原属超级用户的表、序列、视图、函数、类型、检索配置都转给它（扩展自带的对象除外），
# 在主库装 pgvector、zhparser（应用账号装不了扩展；迁移里的 CREATE EXTENSION IF NOT EXISTS 遇到已装的直接跳过）。
# 超级用户（POSTGRES_USER）只留给本机运维：备份、恢复、建库建账号。口令只从环境变量读，不出现在命令行参数里。
set -eu
: "${APP_POSTGRES_PASSWORD:?要设置 APP_POSTGRES_PASSWORD（.env）}"
export APP_USER="${APP_POSTGRES_USER:-consult_app}"
MAIN_DB="${POSTGRES_DB:-consult}"
SU="${POSTGRES_USER:-consult}"
[ "$APP_USER" != "$SU" ] || { echo "应用账号要与超级用户不同" >&2; exit 2; }

psql -v ON_ERROR_STOP=1 -q -U "$SU" -d "$MAIN_DB" <<'SQL'
\getenv appuser APP_USER
\getenv apppw APP_POSTGRES_PASSWORD
SELECT format('CREATE ROLE %I LOGIN', :'appuser') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'appuser') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'appuser', :'apppw') \gexec
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS zhparser;
SELECT format('ALTER DATABASE %I OWNER TO %I', current_database(), :'appuser') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', current_database(), :'appuser') \gexec
-- 已有对象转给应用账号。随表的序列（serial / identity）跟着表走，不单独改；扩展自带的对象不动。
SELECT format('ALTER %s %s OWNER TO %I',
              CASE c.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                             WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
              c.oid::regclass, :'appuser')
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
   AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = :'appuser')
   AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                     AND d.deptype IN ('e', 'a', 'i'))
 ORDER BY c.relkind = 'S', c.oid \gexec
SELECT format('ALTER %s %s OWNER TO %I', CASE p.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END,
              p.oid::regprocedure, :'appuser')
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND p.proowner <> (SELECT oid FROM pg_roles WHERE rolname = :'appuser')
   AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e') \gexec
SELECT format('ALTER TYPE %s OWNER TO %I', t.oid::regtype, :'appuser')
  FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
 WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd')
   AND t.typowner <> (SELECT oid FROM pg_roles WHERE rolname = :'appuser')
   AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e') \gexec
SELECT format('ALTER TEXT SEARCH CONFIGURATION %s OWNER TO %I', c.oid::regconfig, :'appuser')
  FROM pg_ts_config c JOIN pg_namespace n ON n.oid = c.cfgnamespace
 WHERE n.nspname = 'public' AND c.cfgowner <> (SELECT oid FROM pg_roles WHERE rolname = :'appuser') \gexec
SQL
echo "主库 $MAIN_DB 的应用账号 $APP_USER 已就绪"
