#!/bin/bash
# 测试环境初始化：还原面板 → 启动 Web 服务 → loopback API 转发 → 注册站点 → 安装插件
#
# 官方镜像把面板与 LNMP/LAMP 载荷打包在 /www.tar.gz，原始入口 /bt.sh 首启时解压；
# 本 entrypoint 复用该还原逻辑，不启动面板 Web 服务（测试直接以 pyenv 调面板模块）。
set -e

PLUGIN_DIR="/www/server/panel/plugin/sslbt"
BTPY="/www/server/panel/pyenv/bin/python3"
TEST_DOMAIN="${TEST_DOMAIN:-test.example.com}"
MOCK_API_HOST="${MOCK_API_HOST:-mock-api}"
MOCK_API_PORT="${MOCK_API_PORT:-8080}"
LOCAL_API_PORT="${LOCAL_API_PORT:-18080}"

echo "=== sslbt 宝塔插件测试环境 ==="

echo "[1/5] 还原面板与运行环境..."
if [ -f /www.tar.gz ]; then
    tar xzf /www.tar.gz -C / --skip-old-files
    rm -f /www.tar.gz
fi
mkdir -p /www/server/panel/logs /www/wwwroot

echo "[2/5] 启动 Web 服务..."
if [ -f /etc/init.d/nginx ]; then
    /etc/init.d/nginx start || true
elif [ -f /etc/init.d/httpd ]; then
    /etc/init.d/httpd start || true
fi

echo "[3/5] 启动 loopback API 转发: 127.0.0.1:${LOCAL_API_PORT} -> ${MOCK_API_HOST}:${MOCK_API_PORT}"
python3 /forward.py 127.0.0.1 "$LOCAL_API_PORT" "$MOCK_API_HOST" "$MOCK_API_PORT" &

echo "[4/5] 注册测试站点: $TEST_DOMAIN"
mkdir -p "/www/wwwroot/$TEST_DOMAIN"
cd /www/server/panel
TEST_DOMAIN="$TEST_DOMAIN" "$BTPY" - <<'PYEOF'
import json
import os
import sys

sys.path.insert(0, '/www/server/panel/class/')
import public
import panelSite


class Params(dict):
    """宝塔 API 参数对象：同时支持属性与字典访问"""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self[key] = value


domain = os.environ['TEST_DOMAIN']
if public.M('sites').where('name=?', (domain,)).count():
    print('站点已存在，跳过创建: %s' % domain)
else:
    params = Params(
        webname=json.dumps({'domain': domain, 'domainlist': [], 'count': 0}),
        path='/www/wwwroot/%s' % domain,
        port='80',
        type='PHP',
        type_id='0',
        version='00',
        ftp='false',
        sql='false',
        codeing='utf8',
        ps='sslbt-test',
    )
    result = panelSite.panelSite().AddSite(params)
    print('AddSite 返回: %r' % (result,))

if not public.M('sites').where('name=?', (domain,)).count():
    print('错误: 站点未注册进面板数据库')
    sys.exit(1)
print('站点注册完成: %s' % domain)
PYEOF

echo "[5/5] 安装插件..."
mkdir -p "$PLUGIN_DIR"
cp -r /plugin-src/* "$PLUGIN_DIR/"
cd "$PLUGIN_DIR" && bash install.sh install

echo "=== 测试环境就绪 ==="
echo "  API 转发: http://127.0.0.1:${LOCAL_API_PORT} -> ${MOCK_API_HOST}:${MOCK_API_PORT}"
echo "  测试域名: $TEST_DOMAIN"
echo "  插件目录: $PLUGIN_DIR"

# 保持容器运行
exec tail -f /dev/null
