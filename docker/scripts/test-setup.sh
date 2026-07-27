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

docker exec "$CONTAINER" test -f /www/server/panel/plugin/sslbt/scripts/renew-cron.sh && \
    pass "renew-cron.sh 存在" || fail "renew-cron.sh 不存在"

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

# 5. 模拟从旧版升级：旧任务启动的新进程必须自检迁移正文并保留时间。
CRON_SCHEDULE=$(docker exec "$CONTAINER" /www/server/panel/pyenv/bin/python3 -c "
import sqlite3
db = '/www/server/panel/data/db/crontab.db'
conn = sqlite3.connect(db)
row = conn.execute(
    \"SELECT id, where_hour, where_minute FROM crontab\"
    \" WHERE sBody LIKE '%/www/server/panel/plugin/sslbt%' LIMIT 1\"
).fetchone()
legacy = '''#!/bin/bash
cd \"/www/server/panel/plugin/sslbt\"
/www/server/panel/pyenv/bin/python3 -c \"from sslbt_main import sslbt_main; sslbt_main().run_renew_cron(None)\"
'''
conn.execute('UPDATE crontab SET sBody=? WHERE id=?', (legacy, row[0]))
conn.commit()
conn.close()
print('%s:%s' % (row[1], row[2]))
")

HEALTH_RESULT=$(docker exec "$CONTAINER" /www/server/panel/pyenv/bin/python3 -c "
import json
import sys
sys.path.insert(0, '/www/server/panel/class/')
sys.path.insert(0, '/www/server/panel/plugin/sslbt')
from lib.cron import CronManager
result = CronManager('/www/server/panel/plugin/sslbt/data').ensure_healthy()
print(json.dumps(result, ensure_ascii=False))
")

echo "$HEALTH_RESULT" | python3 -c "
import json
import sys
result = json.load(sys.stdin)
if result.get('status') and result.get('changed'):
    print('OK')
else:
    print('FAIL: health check did not repair task: %r' % result)
" | while read line; do
    case "$line" in
        OK)   pass "新进程健康检查已修正旧计划任务" ;;
        FAIL*) fail "计划任务健康检查失败: $line" ;;
    esac
done

CRON_RESULT=$(docker exec "$CONTAINER" /www/server/panel/pyenv/bin/python3 -c "
import json
import sqlite3
db = '/www/server/panel/data/db/crontab.db'
conn = sqlite3.connect(db)
row = conn.execute(
    \"SELECT sBody, where_hour, where_minute FROM crontab\"
    \" WHERE sBody LIKE '%/www/server/panel/plugin/sslbt%' LIMIT 1\"
).fetchone()
conn.close()
print(json.dumps({
    'thin': bool(row and 'scripts/renew-cron.sh' in row[0]),
    'schedule': ('%s:%s' % (row[1], row[2])) if row else '',
}))
")

echo "$CRON_RESULT" | python3 -c "
import json
import sys
result = json.load(sys.stdin)
expected = '$CRON_SCHEDULE'
if result.get('thin') and result.get('schedule') == expected:
    print('OK')
else:
    print('FAIL: expected thin task at %s, got %r' % (expected, result))
" | while read line; do
    case "$line" in
        OK)   pass "旧计划任务运行时自检迁移并保留执行时间" ;;
        FAIL*) fail "旧计划任务迁移失败: $line" ;;
    esac
done

# 6. 获取证书列表（初始为空列表）
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
