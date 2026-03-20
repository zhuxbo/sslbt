#!/bin/bash
# 容器集成测试入口
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

usage() {
    echo "用法: $0 [--server nginx|apache|--all]"
    echo ""
    echo "选项:"
    echo "  --server nginx    运行 Nginx 环境测试"
    echo "  --server apache   运行 Apache 环境测试"
    echo "  --all             运行所有环境测试"
    exit 1
}

run_tests_for() {
    local server="$1"
    local container="baota-${server}"

    echo ""
    echo "=============================="
    echo "  测试环境: $server"
    echo "=============================="

    # 等待容器就绪
    wait_container "$container" || {
        fail "容器 $container 未就绪"
        return 1
    }

    # 重置 Mock API
    reset_mock

    # 运行测试
    echo ""
    echo "--- 插件安装测试 ---"
    bash "$SCRIPT_DIR/test-setup.sh" "$container"

    echo ""
    echo "--- 证书部署测试 ---"
    bash "$SCRIPT_DIR/test-deploy.sh" "$container"

    echo ""
    echo "--- 续签测试 ---"
    bash "$SCRIPT_DIR/test-renew.sh" "$container"
}

# 解析参数
SERVER=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --server) SERVER="$2"; shift 2 ;;
        --all)    SERVER="all"; shift ;;
        -h|--help) usage ;;
        *)        usage ;;
    esac
done

[ -z "$SERVER" ] && usage

case "$SERVER" in
    nginx)  run_tests_for nginx ;;
    apache) run_tests_for apache ;;
    all)
        run_tests_for nginx
        run_tests_for apache
        ;;
    *) usage ;;
esac

summary
exit $?
