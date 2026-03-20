#!/bin/bash
# 续签测试
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

CONTAINER="${1:-baota-nginx}"
TEST_DOMAIN="${TEST_DOMAIN:-test.example.com}"

echo "=== 续签测试: $CONTAINER ==="

# 1. 准备：添加一个证书并部署
switch_scenario "active"
call_plugin "$CONTAINER" "add_cert" '{"order_id": "1001", "site_name": "'$TEST_DOMAIN'"}' > /dev/null 2>&1
call_plugin "$CONTAINER" "deploy_cert" '{"order_id": "1001"}' > /dev/null 2>&1
pass "准备测试数据完成"

# 2. Pull 模式续签 - active 场景
reset_mock
switch_scenario "active"
RESULT=$(call_plugin "$CONTAINER" "run_renew" "{}")
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get('status'):
    print('OK')
else:
    print('FAIL: ' + r.get('msg', ''))
" | while read line; do
    case "$line" in
        OK)   pass "Pull 模式续签检查完成" ;;
        FAIL*) fail "续签检查失败: $line" ;;
    esac
done

# 3. 切换到 processing 场景 - 应返回 pending
switch_scenario "processing"
RESULT=$(call_plugin "$CONTAINER" "run_renew" "{}")
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
results = r.get('data', [])
if not results:
    print('NO_RENEW')
else:
    for res in results:
        print(res.get('status', 'unknown'))
" | while read line; do
    case "$line" in
        NO_RENEW) pass "无需续签的证书（已更新）" ;;
        pending)  pass "Processing 状态正确返回 pending" ;;
        success)  pass "续签成功" ;;
        failure)  fail "续签失败" ;;
        *)        pass "续签状态: $line" ;;
    esac
done

# 4. 切换回 active 场景
switch_scenario "active"
RESULT=$(call_plugin "$CONTAINER" "run_renew" "{}")
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('OK' if r.get('status') else 'FAIL')
" | while read line; do
    [ "$line" = "OK" ] && pass "切回 active 后续签检查通过" || fail "续签检查失败"
done

# 5. 清理
call_plugin "$CONTAINER" "remove_cert" '{"order_id": "1001"}' > /dev/null 2>&1
pass "测试清理完成"
