#!/usr/bin/env bash
# 备份（M5 ④，docs/备份与恢复.md）：数据库、对象存储、审计链头，在部署机仓库根目录执行：
#   BACKUP_PASSPHRASE_FILE=/etc/consult-platform/backup.pass bash scripts/backup.sh [备份根目录]
# 备份根目录：第一个参数 > 环境变量 BACKUP_DIR > ./backups。每次备份一个子目录（UTC 时间戳）：
#   db.dump.gpg       pg_dump 自定义格式，gpg 对称加密（AES-256）
#   kb.dump.gpg       知识区数据库（M5 ③ 分库后才有），同上
#   objects.tar.gpg   MinIO 数据卷打包（对象本身已经服务端加密），再 gpg 加密；备份期间暂停 MinIO 几秒，保证一致
#   manifest.json     时间、迁移版本、审计链头、文件大小和 sha256（不含任何业务内容和密钥）
# 另把链头追加到 备份根目录/audit-heads.log（链外留存，防止审计记录末尾被删后链仍自洽）。
# 超过 BACKUP_KEEP_DAYS（环境变量或 .env，默认 30）天的备份子目录自动删除：已删除项目的资料最迟在这个期限后从备份里消失。
# 备份口令文件只放部署机（权限 600 或 400），和备份文件分开保管；丢了口令备份无法恢复。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DEST_ROOT="${1:-${BACKUP_DIR:-$ROOT/backups}}"
env_val() { { grep -E "^$1=" "$ROOT/.env" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d '[:space:]"'; }   # 没有这一项时返回空（pipefail 下 grep 不报错退出）
KEEP_DAYS="${BACKUP_KEEP_DAYS:-$(env_val BACKUP_KEEP_DAYS)}"
KEEP_DAYS="${KEEP_DAYS:-30}"   # 与后端同一个 .env 值：删除记录据此写“备份中的副本最迟清除日”
PASS_FILE="${BACKUP_PASSPHRASE_FILE:-}"

die() { echo "备份失败：$*" >&2; exit 2; }
[ -n "$PASS_FILE" ] && [ -r "$PASS_FILE" ] || die "要设置 BACKUP_PASSPHRASE_FILE（备份口令文件，只放部署机）"
case "$(stat -c %a "$PASS_FILE")" in 600|400) ;; *) die "备份口令文件权限要是 600 或 400";; esac
[ "$(wc -c < "$PASS_FILE")" -ge 20 ] || die "备份口令至少 20 个字符"
command -v gpg >/dev/null || die "没有 gpg（Ubuntu：apt-get install gnupg）"
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] && [ "$KEEP_DAYS" -ge 1 ] || die "BACKUP_KEEP_DAYS 要是正整数"

enc() {  # 标准输入 → 加密文件
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS_FILE" \
      --symmetric --cipher-algo AES256 --compress-algo none -o "$1"
}

umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dir="$DEST_ROOT/$stamp"
mkdir -p "$dir"
trap 'docker compose unpause minio >/dev/null 2>&1 || true' EXIT

# 1. 先校验审计链，记下链头（不完整就不备份，免得把被改过的数据当成好备份）
out="$(docker compose exec -T backend python -m app.cli verify-audit)" || { echo "$out" >&2; rm -rf "$dir"; die "审计链校验未通过"; }
head_id="$(printf '%s\n' "$out" | sed -n 's/^链头：id=\([0-9]*\).*/\1/p')"
head_hash="$(printf '%s\n' "$out" | sed -n 's/^链头：.*hash=\([0-9a-f]*\).*/\1/p')"
[ -n "$head_hash" ] || { rm -rf "$dir"; die "没有取到审计链头"; }

# 2. 数据库
alembic="$(docker compose exec -T postgres psql -U consult -d consult -tAc 'select version_num from alembic_version' | tr -d '[:space:]')"
docker compose exec -T postgres pg_dump -U consult -d consult -Fc | enc "$dir/db.dump.gpg"
KB_DB="$(env_val KB_POSTGRES_DB)"; KB_DB="${KB_DB:-consult_kb}"
alembic_kb=""
if [ "$(docker compose exec -T postgres psql -U consult -d postgres -tAc "select 1 from pg_database where datname='$KB_DB'" | tr -d '[:space:]')" = "1" ]; then
  alembic_kb="$(docker compose exec -T postgres psql -U consult -d "$KB_DB" -tAc 'select version_num from alembic_version_kb' 2>/dev/null | tr -d '[:space:]' || true)"
  docker compose exec -T postgres pg_dump -U consult -d "$KB_DB" -Fc | enc "$dir/kb.dump.gpg"
fi

# 3. 对象存储（没启用对象存储时跳过）
mid="$(docker compose ps -q minio 2>/dev/null || true)"
if [ -n "$mid" ]; then
  img="$(docker inspect -f '{{.Config.Image}}' "$mid")"
  docker compose pause minio >/dev/null
  docker run --rm --volumes-from "$mid" --user 0 --entrypoint tar "$img" -C /data -cf - . | enc "$dir/objects.tar.gpg"
  docker compose unpause minio >/dev/null
fi

# 4. 清单
files=""
for f in "$dir"/*.gpg; do
  n="$(basename "$f")"
  files="$files\"$n\": {\"bytes\": $(stat -c %s "$f"), \"sha256\": \"$(sha256sum "$f" | cut -d' ' -f1)\"}, "
done
cat > "$dir/manifest.json" <<EOF
{"stamp": "$stamp", "alembic": "$alembic", "alembic_kb": "$alembic_kb", "kb_db": "$KB_DB", "audit_head": {"id": ${head_id:-null}, "hash": "$head_hash"},
 "keep_days": $KEEP_DAYS, "files": {${files%, }}}
EOF
echo "$stamp id=$head_id hash=$head_hash" >> "$DEST_ROOT/audit-heads.log"
total="$(du -sb "$dir" | cut -f1)"
docker compose exec -T backend python -m app.cli backup-record --op create --stamp "$stamp" \
  --head-id "${head_id:-0}" --bytes "$total" >/dev/null

# 5. 清理过期备份
find "$DEST_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -mtime "+$((KEEP_DAYS - 1))" -print -exec rm -rf {} + \
  | sed 's/^/已删除过期备份：/'

echo "备份完成：$dir（$total 字节，迁移 $alembic，审计链头 id=$head_id）"
