#!/bin/bash
set -e

PLUGIN_DIR="/www/server/panel/plugin/sslbt"
TEST_DOMAIN="${TEST_DOMAIN:-test.example.com}"
MOCK_API_URL="${MOCK_API_URL:-http://mock-api:18080}"
MOCK_TOKEN="test-token-1234567890abcdef1234567890ab"

echo "=== sslbt 宝塔插件测试环境 ==="

# 1. 启动宝塔面板
echo "[1/5] 启动宝塔面板..."
/etc/init.d/bt start &

# 2. 等待面板就绪
echo "[2/5] 等待面板就绪..."
for i in $(seq 1 60); do
    if curl -s -o /dev/null http://localhost:8888/ 2>/dev/null; then
        echo "  面板已就绪"
        break
    fi
    sleep 2
done

# 3. 创建测试站点
echo "[3/5] 创建测试站点: $TEST_DOMAIN"
bt site add --name "$TEST_DOMAIN" --path "/www/wwwroot/$TEST_DOMAIN" 2>/dev/null || {
    # 如果 bt 命令不支持，手动创建目录
    mkdir -p "/www/wwwroot/$TEST_DOMAIN"
    echo "  手动创建站点目录"
}

# 4. 安装插件
echo "[4/5] 安装插件..."
mkdir -p "$PLUGIN_DIR"
cp -r /plugin-src/* "$PLUGIN_DIR/"
cd "$PLUGIN_DIR" && bash install.sh install

# 5. 配置插件
echo "[5/5] 配置插件..."
cat > "$PLUGIN_DIR/data/config.json" << EOF
{
    "api_url": "$MOCK_API_URL",
    "api_token": "$MOCK_TOKEN",
    "check_interval_hours": 6,
    "renew_before_days": 13,
    "renew_mode": "pull",
    "version": "1.0"
}
EOF
chmod 0600 "$PLUGIN_DIR/data/config.json"

echo "=== 测试环境就绪 ==="
echo "  Mock API: $MOCK_API_URL"
echo "  测试域名: $TEST_DOMAIN"
echo "  插件目录: $PLUGIN_DIR"

# 保持容器运行
exec tail -f /dev/null
