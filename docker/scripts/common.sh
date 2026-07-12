#!/bin/bash
# 公共函数

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 插件视角的 API 地址：容器内 loopback 转发到 mock-api（API 客户端仅对 loopback 放行 HTTP）
API_URL="http://127.0.0.1:18080"
API_TOKEN="test-token-1234567890abcdef1234567890ab"

# 结果计数走文件：pass/fail 常在管道子 shell 或子脚本里调用，
# 内存变量无法跨进程累加（旧实现 summary 永远是 0，失败也不会让 make 退出非零）
if [ -z "$SSLBT_TEST_RESULTS" ]; then
    SSLBT_TEST_RESULTS="$(mktemp "${TMPDIR:-/tmp}/sslbt-tests.XXXXXX")"
    export SSLBT_TEST_RESULTS
fi

pass() {
    echo "PASS $1" >> "$SSLBT_TEST_RESULTS"
    echo -e "  ${GREEN}✓ PASS${NC}: $1"
    return 0
}

fail() {
    echo "FAIL $1" >> "$SSLBT_TEST_RESULTS"
    echo -e "  ${RED}✗ FAIL${NC}: $1"
    if [ -n "$2" ]; then
        echo -e "    ${RED}$2${NC}"
    fi
    return 0
}

skip() {
    echo "SKIP $1" >> "$SSLBT_TEST_RESULTS"
    echo -e "  ${YELLOW}○ SKIP${NC}: $1"
    return 0
}

summary() {
    local pass_count fail_count skip_count
    pass_count=$(grep -c '^PASS' "$SSLBT_TEST_RESULTS" || true)
    fail_count=$(grep -c '^FAIL' "$SSLBT_TEST_RESULTS" || true)
    skip_count=$(grep -c '^SKIP' "$SSLBT_TEST_RESULTS" || true)
    echo ""
    echo "=============================="
    echo -e "  ${GREEN}通过: $pass_count${NC}"
    [ "$fail_count" -gt 0 ] && echo -e "  ${RED}失败: $fail_count${NC}"
    [ "$skip_count" -gt 0 ] && echo -e "  ${YELLOW}跳过: $skip_count${NC}"
    echo "=============================="
    if [ "$fail_count" -gt 0 ]; then
        grep '^FAIL' "$SSLBT_TEST_RESULTS" | sed 's/^FAIL /  ✗ /'
        return 1
    fi
    return 0
}

# 在容器内执行插件方法
# 用面板自带 pyenv（插件真实运行环境，系统 python3 缺 panelSite/public 依赖）；
# 异常统一转为 JSON 失败输出，避免 docker exec 非零退出触发 set -e 中断整个测试套件
call_plugin() {
    local container="$1"
    local method="$2"
    local params="$3"
    # 注意不能写 ${params:-{}}：bash 会在默认值的首个 } 处截断展开，产生多余右括号
    if [ -z "$params" ]; then
        params='{}'
    fi

    docker exec -w /www/server/panel "$container" /www/server/panel/pyenv/bin/python3 -c "
import sys, json, io, contextlib
sys.path.insert(0, '/www/server/panel/class/')
sys.path.insert(0, '/www/server/panel/plugin/sslbt')

class Params(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)
    def __setattr__(self, key, value):
        self[key] = value

# 面板模块（panelSite/public 等）会向 stdout 打印杂项，全部重定向缓冲，
# 保证本进程 stdout 只有一行 JSON
_buf = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf):
        from sslbt_main import sslbt_main
        plugin = sslbt_main()
        params = Params(**${params})
        result = getattr(plugin, '${method}')(params)
    print(json.dumps(result, ensure_ascii=False, default=str))
except Exception as e:
    print(json.dumps({'status': False, 'msg': '%s: %s' % (type(e).__name__, e)}, ensure_ascii=False))
" 2>/dev/null || true
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
