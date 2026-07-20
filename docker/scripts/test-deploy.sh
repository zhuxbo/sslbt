#!/bin/bash
# 证书部署测试
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

CONTAINER="${1:-baota-nginx}"
TEST_DOMAIN="${TEST_DOMAIN:-test.example.com}"

echo "=== 证书部署测试: $CONTAINER ==="

# 1. 切换 Mock API 到 active 场景
switch_scenario "active"
pass "Mock API 切换到 active 场景"

# 2. 添加证书
RESULT=$(call_plugin "$CONTAINER" "add_cert" '{"order_id": "1001", "site_names": "'$TEST_DOMAIN'", "api_url": "'$API_URL'", "api_token": "'$API_TOKEN'"}')
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get('status'):
    print('OK')
else:
    print('FAIL: ' + r.get('msg', ''))
" | while read line; do
    case "$line" in
        OK)   pass "添加证书 order_id=1001" ;;
        FAIL*) fail "添加证书失败: $line" ;;
    esac
done

# 3. 检查证书状态
RESULT=$(call_plugin "$CONTAINER" "check_cert" '{"order_id": "1001"}')
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
d = r.get('data', {})
if d.get('status') == 'active':
    print('OK')
else:
    print('FAIL: status=' + d.get('status', 'unknown'))
" | while read line; do
    case "$line" in
        OK)   pass "证书状态为 active" ;;
        FAIL*) fail "证书状态异常: $line" ;;
    esac
done

# 4. 部署证书
RESULT=$(call_plugin "$CONTAINER" "deploy_cert" '{"order_id": "1001"}')
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get('status'):
    print('OK')
else:
    print('FAIL: ' + r.get('msg', ''))
" | while read line; do
    case "$line" in
        OK)   pass "证书部署成功" ;;
        FAIL*) fail "证书部署失败: $line" ;;
    esac
done

# 5. 验证回调已发送且状态为 success
sleep 1
CALLBACKS=$(get_callbacks)
echo "$CALLBACKS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
callbacks = data if isinstance(data, list) else data.get('callbacks', [])
entries = [c for c in callbacks if str(c.get('order_id')) == '1001']
if not entries:
    print('FAIL')
elif any(c.get('status') == 'success' for c in entries):
    print('OK')
else:
    print('BAD_STATUS: ' + ','.join(str(c.get('status')) for c in entries))
" 2>/dev/null | while read line; do
    case "$line" in
        OK)   pass "部署回调已发送且状态为 success" ;;
        FAIL) fail "未收到部署回调" ;;
        *)    fail "部署回调状态异常: $line" ;;
    esac
done

# 5.1 失败路径：部署到面板不存在的站点应判失败并回调 failure（P1-13 白名单判定）
RESULT=$(call_plugin "$CONTAINER" "add_cert" '{"order_id": "1004", "site_names": "ghost.example.com", "api_url": "'$API_URL'", "api_token": "'$API_TOKEN'"}')
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('OK' if r.get('status') else 'FAIL: ' + r.get('msg', ''))
" | while read line; do
    case "$line" in
        OK)   pass "添加失败路径测试证书 order_id=1004" ;;
        FAIL*) fail "添加失败路径证书失败: $line" ;;
    esac
done

RESULT=$(call_plugin "$CONTAINER" "deploy_cert" '{"order_id": "1004"}')
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
results = r.get('data') or []
site_failed = bool(results) and all(not item.get('status') for item in results)
print('OK' if site_failed else 'FAIL: %r' % (r,))
" | while read line; do
    case "$line" in
        OK)   pass "不存在站点的部署被正确判为失败" ;;
        FAIL*) fail "失败路径误判: $line" ;;
    esac
done

sleep 1
CALLBACKS=$(get_callbacks)
echo "$CALLBACKS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
callbacks = data if isinstance(data, list) else data.get('callbacks', [])
entries = [c for c in callbacks if str(c.get('order_id')) == '1004']
print('OK' if any(c.get('status') == 'failure' for c in entries) else 'FAIL: %r' % (entries,))
" 2>/dev/null | while read line; do
    case "$line" in
        OK)   pass "失败部署回调 failure 已送达" ;;
        *)    fail "未收到 failure 回调: $line" ;;
    esac
done

call_plugin "$CONTAINER" "remove_cert" '{"order_id": "1004"}' > /dev/null 2>&1

# 6. 验证证书列表
RESULT=$(call_plugin "$CONTAINER" "get_cert_list" "{}")
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
certs = r.get('data', [])
found = any(c.get('order_id') == 1001 for c in certs)
if found:
    cert = [c for c in certs if c.get('order_id') == 1001][0]
    has_deploy = bool(cert.get('metadata', {}).get('last_deploy_at'))
    print('OK' if has_deploy else 'NO_DEPLOY_TIME')
else:
    print('FAIL')
" | while read line; do
    case "$line" in
        OK)            pass "证书列表包含已部署信息" ;;
        NO_DEPLOY_TIME) fail "证书缺少部署时间" ;;
        FAIL)          fail "证书列表中找不到 order_id=1001" ;;
    esac
done

# 7. 删除证书
RESULT=$(call_plugin "$CONTAINER" "remove_cert" '{"order_id": "1001"}')
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
print('OK' if r.get('status') else 'FAIL')
" | while read line; do
    [ "$line" = "OK" ] && pass "删除证书成功" || fail "删除证书失败"
done
