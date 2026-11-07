#!/usr/bin/env bash
# 冒烟测试：在部署机上执行 ./scripts/smoke_test.sh
# 默认测试 http://localhost:8080，可传入其他地址：./scripts/smoke_test.sh http://10.0.0.5:8080
set -u
BASE="${1:-http://localhost:8080}"
pass=0; fail=0

check() {  # 名称 期望状态码 curl 参数...
  local name="$1" expect="$2"; shift 2
  local code
  code=$(curl -s -o /tmp/smoke_body -w '%{http_code}' "$@")
  if [ "$code" = "$expect" ]; then
    echo "  通过  $name"; pass=$((pass+1))
  else
    echo "  失败  $name（期望 $expect，实际 $code）"; head -c 300 /tmp/smoke_body; echo; fail=$((fail+1))
  fi
}

echo "测试地址：$BASE"
check "首页可以打开"            200 "$BASE/"
check "健康检查"                200 "$BASE/api/health"
check "项目列表"                200 "$BASE/api/projects"
check "项目详情 BANK-A"         200 "$BASE/api/projects/BANK-A"
check "不存在的项目返回 404"     404 "$BASE/api/projects/NOPE"
check "正常提问可以通过"         200 -X POST "$BASE/api/chat" -H 'Content-Type: application/json' \
      -d '{"project_code":"BANK-A","message":"一期人力模型怎么选？"}'
check "含口令和 IP 的内容被拦截"  422 -X POST "$BASE/api/chat" -H 'Content-Type: application/json' \
      -d '{"project_code":"BANK-A","message":"核心交换机 10.1.20.1 密码：Abc@12345"}'

echo
echo "各组件状态："
curl -s "$BASE/api/health"; echo
echo
echo "结果：通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ]
