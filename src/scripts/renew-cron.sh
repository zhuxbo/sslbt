#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PLUGIN_DIR/data/logs/cron.log"

# cron.log 轮转：超过 1000 行保留最后 500 行
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt 1000 ]; then
    tail -500 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

cd "$PLUGIN_DIR" || exit 1

# 优先使用注册时已验证的面板解释器；路径失效时回退 PATH 中的 python3。
PY_BIN="${1:-/www/server/panel/pyenv/bin/python3}"
[ -x "$PY_BIN" ] || PY_BIN="$(command -v python3)"
if [ -z "$PY_BIN" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 未找到可用的 Python 解释器" >> "$LOG_FILE"
    exit 127
fi

"$PY_BIN" - <<'PY' >> "$LOG_FILE" 2>&1
import os
import sys
import time

sys.path.insert(0, '/www/server/panel/class/')
sys.path.insert(0, os.getcwd())

from sslbt_main import sslbt_main

plugin = sslbt_main()
result = plugin.run_renew_cron(None)
# run_renew_cron 会把异常转为错误结果；必须打印，否则计划任务日志无法呈现本轮结论。
print('[%s] %s' % (
    time.strftime('%Y-%m-%d %H:%M:%S'),
    (result or {}).get('msg', '无返回'),
))
PY
