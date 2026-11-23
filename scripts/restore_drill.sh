#!/usr/bin/env bash
# 恢复演练（三期 G1②，docs/备份与恢复.md 第 5 节）：把一个备份恢复到临时库，核对后删掉临时库。
# 不停服务、不动当前数据库和对象存储，生产可以照常跑。
#   BACKUP_PASSPHRASE_FILE=/etc/consult-platform/backup.pass bash scripts/restore_drill.sh /srv/backups/consult-platform/20270110T020000Z
#   DRILL_STRICT=1 ...   刚备份完、期间无人使用时用：每张表的行数和内容哈希都要与当前库一致（审计表除外）
# 核对项（任一不过退出码 2）：清单 sha256 → 解密 → pg_restore 到 consult_drill（知识区 consult_kb_drill）→
#   迁移版本与清单一致 → 表的集合与当前库一致 → 临时库上 verify-audit：链完整且备份时的链头在链上 →
#   对象存储归档能完整解开、文件数 → 逐表行数和内容哈希与当前库对比（只输出表名、行数、是否一致，不输出内容）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SRC="${1:?用法：bash scripts/restore_drill.sh <备份目录>}"
SRC="${SRC%/}"
PASS_FILE="${BACKUP_PASSPHRASE_FILE:-}"
STRICT="${DRILL_STRICT:-0}"
stamp="$(basename "$SRC")"
DRILL_DB=consult_drill
DRILL_KB=consult_kb_drill

die() { echo "演练不通过：$*" >&2; exit 2; }
[ -f "$SRC/manifest.json" ] && [ -f "$SRC/db.dump.gpg" ] || die "$SRC 不是完整的备份目录"
[ -n "$PASS_FILE" ] && [ -r "$PASS_FILE" ] || die "要设置 BACKUP_PASSPHRASE_FILE"
command -v gpg >/dev/null || die "没有 gpg"

manifest() {
  python3 -c 'import json,sys
v = json.load(open(sys.argv[1], encoding="utf-8"))
for k in sys.argv[2:]:
    v = v[k]
print("" if v is None else v)' "$SRC/manifest.json" "$@"
}
psql_su() { docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U consult -qtA "$@" </dev/null; }   # 不读 stdin：否则会吃掉下面 while read 的输入，只比到第一张表

started_at="$(date +%s)"
for f in "$SRC"/*.gpg; do
  n="$(basename "$f")"
  [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$(manifest files "$n" sha256)" ] || die "$n 的 sha256 与清单不一致"
done
echo "1. 清单校验通过：备份 $stamp，迁移 $(manifest alembic)"

umask 077
tmp="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp"
  psql_su -d postgres -c "DROP DATABASE IF EXISTS $DRILL_DB WITH (FORCE)" >/dev/null 2>&1 || true
  psql_su -d postgres -c "DROP DATABASE IF EXISTS $DRILL_KB WITH (FORCE)" >/dev/null 2>&1 || true
}
trap cleanup EXIT
dec() { gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS_FILE" -d "$1" > "$2"; }
dec "$SRC/db.dump.gpg" "$tmp/db.dump" || die "解密失败（口令不对或文件损坏）"
[ -f "$SRC/kb.dump.gpg" ] && { dec "$SRC/kb.dump.gpg" "$tmp/kb.dump" || die "知识区备份解密失败"; }
[ -f "$SRC/objects.tar.gpg" ] && { dec "$SRC/objects.tar.gpg" "$tmp/objects.tar" || die "对象存储备份解密失败"; }
echo "2. 解密通过"

restore_to() {  # 库名 dump文件
  psql_su -d postgres -c "DROP DATABASE IF EXISTS $1 WITH (FORCE)" >/dev/null
  psql_su -d postgres -c "CREATE DATABASE $1 OWNER consult" >/dev/null
  docker compose exec -T postgres pg_restore -U consult -d "$1" --exit-on-error < "$2" || die "pg_restore 到 $1 失败"
}
restore_to "$DRILL_DB" "$tmp/db.dump"
kbdb="$(manifest kb_db)"; kbdb="${kbdb:-consult_kb}"
[ -f "$tmp/kb.dump" ] && restore_to "$DRILL_KB" "$tmp/kb.dump"
echo "3. 已恢复到临时库 $DRILL_DB$([ -f "$tmp/kb.dump" ] && echo "、$DRILL_KB")"

got="$(psql_su -d "$DRILL_DB" -c 'select version_num from alembic_version' | tr -d '[:space:]')"
[ "$got" = "$(manifest alembic)" ] || die "临时库迁移版本 $got 与清单 $(manifest alembic) 不一致"
if [ -f "$tmp/kb.dump" ] && [ -n "$(manifest alembic_kb)" ]; then
  got="$(psql_su -d "$DRILL_KB" -c 'select version_num from alembic_version_kb' | tr -d '[:space:]')"
  [ "$got" = "$(manifest alembic_kb)" ] || die "知识区临时库迁移版本 $got 与清单不一致"
fi
echo "4. 迁移版本与清单一致"

tables() { psql_su -d "$1" -c "select tablename from pg_tables where schemaname='public' order by 1"; }
fingerprint() {  # 库名 表名 → "行数 内容哈希"（行文本各自 md5 后排序再 md5，与行顺序无关）
  psql_su -d "$1" -c "select count(*) || ' ' || coalesce(md5(string_agg(h, '' order by h)), '-')
                      from (select md5(t::text) h from \"$2\" t) s"
}
fail=0
compare() {  # 当前库 临时库
  if [ "$(tables "$1")" != "$(tables "$2")" ]; then
    echo "   $1：表的集合与临时库不一致"; fail=1; return
  fi
  while read -r t; do
    [ -n "$t" ] || continue
    a="$(fingerprint "$1" "$t")"; b="$(fingerprint "$2" "$t")"
    if [ "$a" = "$b" ]; then
      printf '   %-40s 一致（%s 行）\n' "$1.$t" "${b%% *}"
    else
      printf '   %-40s 当前 %s 行 / 备份 %s 行，内容不同\n' "$1.$t" "${a%% *}" "${b%% *}"
      [ "$STRICT" = 1 ] && [ "$t" != audit_logs ] && fail=1
    fi
  done <<< "$(tables "$2")"
}

# 临时库上校验审计链：后端账号要能连临时库（verify-audit 会写一条校验记录，写在临时库里）
app_user="$(docker compose exec -T backend python -c 'import os; from sqlalchemy import make_url; print(make_url(os.environ["DATABASE_URL"]).username)' | tr -d '[:space:]')"
psql_su -d "$DRILL_DB" -c "GRANT CONNECT ON DATABASE $DRILL_DB TO \"$app_user\"; GRANT USAGE ON SCHEMA public TO \"$app_user\";
  GRANT ALL ON ALL TABLES IN SCHEMA public TO \"$app_user\"; GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO \"$app_user\"" >/dev/null
# 先做逐表对比再跑 verify-audit（它会往临时库审计表写一条）
echo "5. 逐表对比（DRILL_STRICT=$STRICT；审计表另由第 6 步按哈希链核对）"
compare consult "$DRILL_DB"
[ -f "$tmp/kb.dump" ] && compare "$kbdb" "$DRILL_KB"
[ "$fail" = 0 ] || die "逐表对比不一致（见上）"

head_hash="$(manifest audit_head hash)"
docker compose exec -T -e DRILL_DB="$DRILL_DB" -e EXPECT="$head_hash" backend sh -c \
  'DATABASE_URL="${DATABASE_URL%/*}/$DRILL_DB" python -m app.cli verify-audit --expect-head "$EXPECT"' \
  || die "临时库审计链校验未通过（备份时的链头 $head_hash 不在链上）"
echo "6. 临时库审计链完整，备份时的链头在链上"

if [ -f "$tmp/objects.tar" ]; then
  n="$(tar -tf "$tmp/objects.tar" | grep -cv '/$' || true)"
  echo "7. 对象存储归档完整解开：$n 个文件"
else
  echo "7. 备份里没有对象存储，跳过"
fi

echo "演练通过：备份 $stamp，用时 $(( $(date +%s) - started_at )) 秒（RTO 4 小时以内为通过）。临时库已删除"
