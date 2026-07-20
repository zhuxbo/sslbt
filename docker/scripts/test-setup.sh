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

# 4. 读取插件配置
RESULT=$(call_plugin "$CONTAINER" "get_config" "{}")
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get('status') and r.get('data', {}).get('plugin_version'):
    print('OK')
else:
    print('FAIL: ' + r.get('msg', ''))
" | while read line; do
    case "$line" in
        OK)   pass "插件配置读取成功" ;;
        FAIL*) fail "插件配置读取失败: $line" ;;
    esac
done

# 5. 获取证书列表（初始为空列表）
RESULT=$(call_plugin "$CONTAINER" "get_cert_list" "{}")
echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get('status') and isinstance(r.get('data'), list):
    print('OK')
else:
    print('FAIL: ' + r.get('msg', ''))
" | while read line; do
    case "$line" in
        OK)   pass "证书列表接口正常" ;;
        FAIL*) fail "证书列表接口异常: $line" ;;
    esac
done
