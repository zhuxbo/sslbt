#!/bin/bash

PLUGIN_DIR="/www/server/panel/plugin/sslbt"
DATA_DIR="$PLUGIN_DIR/data"
LOG_DIR="$DATA_DIR/logs"
CERTS_DIR="$DATA_DIR/certs"
PENDING_KEYS_DIR="$DATA_DIR/pending-keys"

# 宝塔自带解释器；系统 python3 缺 psutil 等面板依赖，用它注册计划任务会烧出一个
# 每天必然失败的脚本，且脚本内的 [ -x ] 检查恰好通过、回退分支永不触发
PANEL_PYTHON="/www/server/panel/pyenv/bin/python3"

# 注册/刷新计划任务：保留已有执行时间，任务不存在时才新建。
# 拿不到面板解释器就放弃——宁可不注册，也不注册一个跑不通的任务。
setup_cron() {
    if [ ! -x "$PANEL_PYTHON" ]; then
        echo "未找到面板解释器 $PANEL_PYTHON，跳过计划任务注册"
        return 0
    fi
    "$PANEL_PYTHON" -c "
import sys
sys.path.insert(0, '/www/server/panel/class/')
sys.path.insert(0, '$PLUGIN_DIR')
from lib.cron import CronManager
res = CronManager('$DATA_DIR').refresh()
print('计划任务: ' + str(res.get('message', '')))
sys.exit(0 if res.get('status') else 1)
" || echo "计划任务注册失败，请在插件设置页手动设置"
}

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

    # 注册/刷新计划任务（deploy-spec §7.3 步骤 8 / §7.4 幂等性）：
    # 此前安装与升级都不碰计划任务，宝塔重装/备份恢复/跨机迁移后证书齐全但 cron 从来没有，
    # 而 add_cert 只在"第一次且不存在"时才建，已有证书的用户永远等不到
    setup_cron

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
