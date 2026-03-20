#!/bin/bash
# 公共函数

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    echo -e "  ${GREEN}✓ PASS${NC}: $1"
}

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo -e "  ${RED}✗ FAIL${NC}: $1"
    [ -n "$2" ] && echo -e "    ${RED}$2${NC}"
}

skip() {
    SKIP_COUNT=$((SKIP_COUNT + 1))
    echo -e "  ${YELLOW}○ SKIP${NC}: $1"
}

summary() {
    echo ""
    echo "=============================="
    echo -e "  ${GREEN}通过: $PASS_COUNT${NC}"
    [ $FAIL_COUNT -gt 0 ] && echo -e "  ${RED}失败: $FAIL_COUNT${NC}"
    [ $SKIP_COUNT -gt 0 ] && echo -e "  ${YELLOW}跳过: $SKIP_COUNT${NC}"
    echo "=============================="
    return $FAIL_COUNT
}

# 在容器内执行插件方法
call_plugin() {
    local container="$1"
    local method="$2"
    local params="$3"

    docker exec "$container" python3 -c "
import sys, json
sys.path.insert(0, '/www/server/panel/class/')
sys.path.insert(0, '/www/server/panel/plugin/sslbt')

class Params:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

from sslbt_main import sslbt_main
plugin = sslbt_main()
params = Params(**${params:-{}})
result = plugin.${method}(params)
print(json.dumps(result, ensure_ascii=False))
" 2>/dev/null
}

# 切换 Mock API 场景
switch_scenario() {
    local scenario="$1"
    curl -s -X POST "http://localhost:18080/admin/scenario/${scenario}" > /dev/null 2>&1
}

# 重置 Mock API
reset_mock() {
    curl -s -X POST "http://localhost:18080/admin/reset" > /dev/null 2>&1
}

# 获取 Mock API 回调记录
get_callbacks() {
    curl -s "http://localhost:18080/admin/callbacks" 2>/dev/null
}

# 等待容器就绪
wait_container() {
    local container="$1"
    local max_wait="${2:-120}"
    echo "等待容器 $container 就绪..."
    for i in $(seq 1 $max_wait); do
        if docker exec "$container" test -f /www/server/panel/plugin/sslbt/sslbt_main.py 2>/dev/null; then
            echo "容器 $container 就绪"
            return 0
        fi
        sleep 2
    done
    echo "容器 $container 超时未就绪"
    return 1
}
