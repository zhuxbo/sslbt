#!/bin/bash
# 插件安装测试
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

CONTAINER="${1:-baota-nginx}"

echo "=== 插件安装测试: $CONTAINER ==="

# 1. 验证插件文件完整性
docker exec "$CONTAINER" test -f /www/server/panel/plugin/sslbt/sslbt_main.py && \
    pass "sslbt_main.py 存在" || fail "sslbt_main.py 不存在"

docker exec "$CONTAINER" test -f /www/server/panel/plugin/sslbt/index.html && \
    pass "index.html 存在" || fail "index.html 不存在"

docker exec "$CONTAINER" test -f /www/server/panel/plugin/sslbt/info.json && \
    pass "info.json 存在" || fail "info.json 不存在"

docker exec "$CONTAINER" test -d /www/server/panel/plugin/sslbt/lib && \
    pass "lib/ 目录存在" || fail "lib/ 目录不存在"

# 2. 验证 data 目录和配置
docker exec "$CONTAINER" test -d /www/server/panel/plugin/sslbt/data && \
    pass "data/ 目录存在" || fail "data/ 目录不存在"

docker exec "$CONTAINER" test -f /www/server/panel/plugin/sslbt/data/config.json && \
    pass "config.json 存在" || fail "config.json 不存在"

docker exec "$CONTAINER" test -d /www/server/panel/plugin/sslbt/data/logs && \
    pass "logs/ 目录存在" || fail "logs/ 目录不存在"

# 3. 验证文件权限
PERM=$(docker exec "$CONTAINER" stat -c '%a' /www/server/panel/plugin/sslbt/data/config.json 2>/dev/null || echo "unknown")
[ "$PERM" = "600" ] && pass "config.json 权限 0600" || fail "config.json 权限错误: $PERM"

# 4. 测试 API 连接
RESULT=$(call_plugin "$CONTAINER" "test_connection" "{}")
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get('status'):
    print('CONN_OK')
else:
    print('CONN_FAIL: ' + r.get('msg', ''))
" | while read line; do
    case "$line" in
        CONN_OK)   pass "API 连接测试成功" ;;
        CONN_FAIL*) fail "API 连接测试失败: $line" ;;
    esac
done

# 5. 获取仪表板数据
RESULT=$(call_plugin "$CONTAINER" "get_dashboard" "{}")
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get('status') and r.get('data', {}).get('api_configured'):
    print('OK')
else:
    print('FAIL')
" | while read line; do
    [ "$line" = "OK" ] && pass "仪表板数据加载成功" || fail "仪表板数据加载失败"
done
