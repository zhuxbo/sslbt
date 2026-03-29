#!/bin/bash

PLUGIN_DIR="/www/server/panel/plugin/sslbt"
DATA_DIR="$PLUGIN_DIR/data"
LOG_DIR="$DATA_DIR/logs"
CERTS_DIR="$DATA_DIR/certs"
PENDING_KEYS_DIR="$DATA_DIR/pending-keys"

install() {
    # 创建数据目录
    mkdir -p "$DATA_DIR" "$LOG_DIR" "$CERTS_DIR" "$PENDING_KEYS_DIR"

    # 首次安装：创建默认配置（已有配置保留，由代码自动迁移）
    if [ ! -f "$DATA_DIR/config.json" ]; then
        cat > "$DATA_DIR/config.json" << 'EOF'
{
    "release_url": "",
    "upgrade_channel": "main",
    "schedule": {
        "renew_mode": "pull",
        "renew_before_days": 14
    },
    "certificates": []
}
EOF
    fi

    # 设置权限
    chmod 0700 "$DATA_DIR" "$LOG_DIR" "$CERTS_DIR" "$PENDING_KEYS_DIR"
    chmod 0600 "$DATA_DIR/config.json"

    echo "sslbt 插件安装完成"
}

uninstall() {
    # 删除计划任务（保留 data 目录）
    if [ -f "$PLUGIN_DIR/lib/cron.py" ]; then
        cd "$PLUGIN_DIR" && python3 -c "
import sys
sys.path.insert(0, '/www/server/panel/class/')
sys.path.insert(0, '$PLUGIN_DIR')
try:
    from lib.cron import CronManager
    CronManager('$DATA_DIR').remove()
except Exception:
    pass
" 2>/dev/null
    fi
    echo "sslbt 插件已卸载（数据已保留在 $DATA_DIR）"
}

case "$1" in
    install)   install ;;
    uninstall) uninstall ;;
    *)         install ;;
esac
