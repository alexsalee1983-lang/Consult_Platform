#!/usr/bin/env bash
# 冒烟测试：在部署机上执行 ./scripts/smoke_test.sh
# 测试地址：第一个参数 > 环境变量 SMOKE_BASE > 默认 http://localhost:8080，例如
#   ./scripts/smoke_test.sh http://127.0.0.1:18080
#   SMOKE_BASE=http://127.0.0.1:18080 ./scripts/smoke_test.sh
# 接了模型（含模拟模型）时加 SMOKE_EXPECT_LLM=ok，多检查一项“模型可用”（共 23 项，否则 22 项）。
#
# 需要测试账号（只在 APP_ENV=dev/test 时由后端创建）：从环境变量读取，没有就读仓库根目录的 .env：
#   TEST_ACCOUNT_USERNAME / TEST_ACCOUNT_PASSWORD / TEST_ACCOUNT_TOTP_SECRET
# 动态码用 python3 标准库现算，不需要额外安装。
set -u
BASE="${1:-${SMOKE_BASE:-http://localhost:8080}}"
BASE="${BASE%/}"
echo "测试地址：$BASE"
# 先确认地址能连上，避免地址填错时每一项都报失败（状态码 000）
if ! curl -s -o /dev/null --max-time 10 "$BASE/api/health"; then
  echo "连不上 $BASE：检查地址和端口（第一个参数或 SMOKE_BASE），以及服务是否已启动（docker compose ps）" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
JAR="$(mktemp)"; BODY="$(mktemp)"
trap 'rm -f "$JAR" "$BODY"' EXIT

env_val() {  # 先取环境变量，再取 .env
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f "$ROOT/.env" ]; then
    v="$(grep -E "^$1=" "$ROOT/.env" | tail -1 | cut -d= -f2-)"
  fi
  printf '%s' "$v"
}
USER_NAME="$(env_val TEST_ACCOUNT_USERNAME)"
USER_PASS="$(env_val TEST_ACCOUNT_PASSWORD)"
TOTP_SECRET="$(env_val TEST_ACCOUNT_TOTP_SECRET)"

totp() {
  python3 - "$TOTP_SECRET" <<'PY'
import base64, hashlib, hmac, struct, sys, time
s = sys.argv[1].strip().upper(); s += "=" * (-len(s) % 8)
h = hmac.new(base64.b32decode(s), struct.pack(">Q", int(time.time()) // 30), hashlib.sha1).digest()
o = h[-1] & 15
print("%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000))
PY
}

json_field() {  # 从响应体取一个字段
  python3 -c "import json,sys; print(json.load(open(sys.argv[1], encoding='utf-8')).get(sys.argv[2], ''))" "$BODY" "$1"
}

check() {  # 名称 期望状态码 curl 参数...
  local name="$1" expect="$2"; shift 2
  local code
  code=$(curl -s -o "$BODY" -w '%{http_code}' -b "$JAR" -c "$JAR" "$@")
  if [ "$code" = "$expect" ]; then
    echo "  通过  $name"; pass=$((pass+1))
  else
    echo "  失败  $name（期望 $expect，实际 $code）"; head -c 300 "$BODY"; echo; fail=$((fail+1))
  fi
}

login_body() {
  python3 -c 'import json,sys; print(json.dumps({"username": sys.argv[1], "password": sys.argv[2], "totp": sys.argv[3]}))' \
    "$USER_NAME" "$USER_PASS" "$(totp)"
}

if [ -z "$USER_NAME" ] || [ -z "$USER_PASS" ] || [ -z "$TOTP_SECRET" ]; then
  echo "  失败  未配置测试账号（TEST_ACCOUNT_USERNAME / PASSWORD / TOTP_SECRET），无法测试登录后的接口"
  exit 1
fi

J='Content-Type: application/json'
check "首页可以打开"                 200 "$BASE/"
check "健康检查不需要登录"            200 "$BASE/api/health"
storage="$(curl -s "$BASE/api/health" | python3 -c "import json,sys; print(json.load(sys.stdin)['checks'].get('object_storage', ''))")"
if [ "$storage" = "ok" ]; then echo "  通过  对象存储可用（两区分桶、服务端加密）"; pass=$((pass+1)); else
  echo "  失败  对象存储状态：$storage"; fail=$((fail+1)); fi
check "未登录访问项目列表被拒绝"       401 "$BASE/api/projects"
check "未登录提问被拒绝"              401 -X POST "$BASE/api/chat" -H "$J" -d '{"project_code":"BANK-A","message":"x"}'

# 同一个动态码不能用两次：30 秒内重复运行时等到下一个时间步再登录
code=$(curl -s -o "$BODY" -w '%{http_code}' -c "$JAR" -X POST "$BASE/api/auth/login" -H "$J" -d "$(login_body)")
if [ "$code" = "401" ]; then
  sleep $(( 31 - $(date +%s) % 30 ))
  code=$(curl -s -o "$BODY" -w '%{http_code}' -c "$JAR" -X POST "$BASE/api/auth/login" -H "$J" -d "$(login_body)")
fi
if [ "$code" = "200" ]; then echo "  通过  测试账号登录"; pass=$((pass+1)); else
  echo "  失败  测试账号登录（期望 200，实际 $code）"; head -c 300 "$BODY"; echo; fail=$((fail+1)); fi
CSRF="$(json_field csrf_token)"
C="X-CSRF-Token: $CSRF"

check "项目列表"                     200 "$BASE/api/projects"
check "项目详情 BANK-A"              200 "$BASE/api/projects/BANK-A"
check "不存在的项目返回 404"          404 "$BASE/api/projects/NOPE"
# 测试账号故意不是 GRID-B 的成员（app/core/bootstrap.py TEST_MEMBERSHIPS）
check "非项目成员访问 GRID-B 返回 404" 404 "$BASE/api/projects/GRID-B"
check "顾问不能查看审计日志"          403 "$BASE/api/audit"
check "顾问不能校验审计链"            403 "$BASE/api/audit/verify"
check "顾问不能管理账号"              403 "$BASE/api/admin/users"
check "缺少 CSRF 令牌的提问被拒绝"    403 -X POST "$BASE/api/chat" -H "$J" \
      -d '{"project_code":"BANK-A","message":"一期人力模型怎么选？"}'
check "正常提问可以通过"              200 -X POST "$BASE/api/chat" -H "$J" -H "$C" \
      -d '{"project_code":"BANK-A","message":"一期人力模型怎么选？"}'
check "含口令和 IP 的内容被拦截"      422 -X POST "$BASE/api/chat" -H "$J" -H "$C" \
      -d '{"project_code":"BANK-A","message":"核心交换机 10.1.20.1 密码：Abc@12345"}'
check "咨询师 Agent 能读项目区"       200 -X POST "$BASE/api/agents/consultant-lead/tools/project.read" -H "$J" -H "$C" \
      -d '{"project_code":"BANK-A"}'
check "知识侧 Agent 读不到项目区"     403 -X POST "$BASE/api/agents/kb-builder/tools/project.read" -H "$J" -H "$C" \
      -d '{"project_code":"BANK-A"}'
check "流式提问可以通过"              200 -X POST "$BASE/api/chat/stream" -H "$J" -H "$C" \
      -d '{"project_code":"BANK-A","message":"一期人力模型怎么选？"}'
if grep -q "^event: done" "$BODY"; then echo "  通过  流式输出正常结束"; pass=$((pass+1)); else
  echo "  失败  流式输出没有正常结束"; head -c 300 "$BODY"; echo; fail=$((fail+1)); fi
# 接了模型（含 CI 的模拟模型）时设 SMOKE_EXPECT_LLM=ok，要求模型可用、回复不是演示模式
if [ "${SMOKE_EXPECT_LLM:-}" = "ok" ]; then
  llm="$(curl -s "$BASE/api/health" | python3 -c "import json,sys; print(json.load(sys.stdin)['checks']['llm'])")"
  if [ "$llm" = "ok" ] && ! grep -q '"demo": true' "$BODY"; then echo "  通过  模型可用且不是演示回复"; pass=$((pass+1)); else
    echo "  失败  模型状态：$llm"; fail=$((fail+1)); fi
fi
check "退出登录"                     204 -X POST "$BASE/api/auth/logout" -H "$C"
check "退出后旧会话失效"              401 "$BASE/api/projects"

echo
echo "各组件状态："
curl -s "$BASE/api/health"; echo
echo
echo "结果：通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ]
