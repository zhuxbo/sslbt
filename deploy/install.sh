#!/bin/bash
# sslbt 宝塔面板插件安装脚本
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
echo_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
echo_error() { echo -e "${RED}[ERROR]${NC} $1"; }

CHANNEL=""
TARGET_VERSION=""
FORCE=false
RELEASE_HOST=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dev)    CHANNEL="dev"; shift ;;
        --main)   CHANNEL="main"; shift ;;
        --version)
            [ -z "${2:-}" ] && { echo_error "--version 需要指定版本号"; exit 1; }
            TARGET_VERSION="$2"; shift 2 ;;
        --force)  FORCE=true; shift ;;
        --help|-h)
            echo "用法: curl -fsSL <url>/install.sh | bash -s -- [host] [选项]"
            echo ""
            echo "参数:"
            echo "  [host]         发布服务器（域名或域名+路径）"
            echo ""
            echo "选项:"
            echo "  --dev          安装测试版"
            echo "  --main         安装稳定版（默认）"
            echo "  --version VER  安装指定版本"
            echo "  --force        强制重新安装"
            echo "  --help         显示帮助"
            exit 0 ;;
        --*)     echo_error "未知参数: $1"; exit 1 ;;
        *)
            [ -z "$RELEASE_HOST" ] && RELEASE_HOST="$1" || { echo_error "多余参数: $1"; exit 1; }
            shift ;;
    esac
done

RELEASE_PATH="/sslbt"
FALLBACK_HOST="release.cnssl.com"
if [ -n "$RELEASE_HOST" ]; then
    RELEASE_URL="https://${RELEASE_HOST%/}${RELEASE_PATH}"
elif [ -n "${SSLBT_RELEASE_URL:-}" ]; then
    RELEASE_URL="${SSLBT_RELEASE_URL%/}"
else
    RELEASE_URL="https://${FALLBACK_HOST}${RELEASE_PATH}"
    echo_warn "未指定服务器，使用默认: $RELEASE_URL"
fi

[ "$EUID" -ne 0 ] && { echo_error "请使用 root 权限运行"; exit 1; }

PANEL_DIR="/www/server/panel"
PLUGIN_DIR="$PANEL_DIR/plugin/sslbt"
[ ! -d "$PANEL_DIR" ] && { echo_error "未检测到宝塔面板: $PANEL_DIR"; exit 1; }
echo_info "宝塔面板: $PANEL_DIR"

normalize_version() {
    local ver="$1"
    [[ "$ver" != v* ]] && echo "v$ver" || echo "$ver"
}

# 下载 releases.json（缓存复用）
echo_info "获取版本信息..."
RELEASES_JSON=$(curl -s --connect-timeout 10 "$RELEASE_URL/releases.json" 2>/dev/null)

get_target_version() {
    if [ -n "$TARGET_VERSION" ]; then
        [ -z "$CHANNEL" ] && { [[ "$TARGET_VERSION" == *"-"* ]] && CHANNEL="dev" || CHANNEL="main"; }
        normalize_version "$TARGET_VERSION"
        return
    fi

    [ -z "$RELEASES_JSON" ] && { echo ""; return; }
    [ -z "$CHANNEL" ] && CHANNEL="main"

    local version=""
    if [ "$CHANNEL" = "dev" ]; then
        version=$(echo "$RELEASES_JSON" | grep -o '"latest_dev" *: *"[^"]*"' | cut -d'"' -f4)
    else
        version=$(echo "$RELEASES_JSON" | grep -o '"latest_main" *: *"[^"]*"' | cut -d'"' -f4)
    fi
    echo "$version"
}

VERSION=$(get_target_version)
[ -z "$VERSION" ] && { echo_error "无法获取版本信息: $RELEASE_URL/releases.json"; exit 1; }
[ -z "$CHANNEL" ] && { [[ "$VERSION" == *"-"* ]] && CHANNEL="dev" || CHANNEL="main"; }
[ "$CHANNEL" = "dev" ] && echo_info "目标版本: $VERSION (测试版)" || echo_info "目标版本: $VERSION (稳定版)"

# 检测已安装版本
CURRENT_VERSION=""
if [ -f "$PLUGIN_DIR/info.json" ]; then
    CURRENT_VERSION=$(python3 -c "
import json
try:
    v = json.load(open('$PLUGIN_DIR/info.json'))['versions']
    v = v if v.startswith('v') else 'v'+v
    print(v)
except: pass
" 2>/dev/null || echo "")
fi

if [ -n "$CURRENT_VERSION" ]; then
    if [ "$CURRENT_VERSION" = "$VERSION" ]; then
        [ "$FORCE" = true ] && echo_info "当前版本: $CURRENT_VERSION，强制重新安装" || { echo_info "当前版本 $CURRENT_VERSION 已是目标版本，使用 --force 强制重新安装"; exit 0; }
    else
        echo_info "升级: $CURRENT_VERSION → $VERSION"
    fi
fi

DOWNLOAD_URL="$RELEASE_URL/$CHANNEL/$VERSION/sslbt.zip"
TMP_FILE="/tmp/sslbt-$VERSION.zip"

echo_info "下载 sslbt.zip..."
if ! curl -fsSL --connect-timeout 30 "$DOWNLOAD_URL" -o "$TMP_FILE" 2>/dev/null; then
    echo_error "下载失败: $DOWNLOAD_URL"
    rm -f "$TMP_FILE"
    exit 1
fi

# SHA256 校验（从缓存的 RELEASES_JSON 中提取，避免重复下载）
EXPECTED_HASH=""
if [ -n "$RELEASES_JSON" ] && command -v python3 >/dev/null 2>&1; then
    EXPECTED_HASH=$(echo "$RELEASES_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    h = d.get('versions',{}).get('$VERSION',{}).get('checksums',{}).get('sslbt.zip','')
    if h.startswith('sha256:'):
        print(h[7:])
except: pass
" 2>/dev/null)
fi

if [ -n "$EXPECTED_HASH" ]; then
    ACTUAL_HASH=$(sha256sum "$TMP_FILE" 2>/dev/null | cut -d' ' -f1)
    [ -z "$ACTUAL_HASH" ] && ACTUAL_HASH=$(shasum -a 256 "$TMP_FILE" 2>/dev/null | cut -d' ' -f1)
    if [ -z "$ACTUAL_HASH" ]; then
        echo_error "无法计算 SHA256"
        rm -f "$TMP_FILE"
        exit 1
    fi
    if [ "$ACTUAL_HASH" != "$EXPECTED_HASH" ]; then
        echo_error "SHA256 校验失败"
        echo_error "  期望: $EXPECTED_HASH"
        echo_error "  实际: $ACTUAL_HASH"
        rm -f "$TMP_FILE"
        exit 1
    fi
    echo_info "SHA256 校验通过"
else
    echo_warn "无法获取校验和，跳过 SHA256 校验"
fi

echo_info "安装中..."

if [ "$FORCE" = true ] && [ -d "$PLUGIN_DIR" ]; then
    # --force 模式：备份旧目录到 /tmp，全新安装
    BACKUP_DIR="/tmp/sslbt.bak.$(date +%Y%m%d%H%M%S)"
    echo_info "备份旧目录: $BACKUP_DIR"
    mv "$PLUGIN_DIR" "$BACKUP_DIR"
    mkdir -p "$PLUGIN_DIR"
    unzip -o "$TMP_FILE" -d "$PLUGIN_DIR" >/dev/null
    # 从备份恢复 data/
    if [ -d "$BACKUP_DIR/data" ]; then
        echo_info "恢复 data/ 目录..."
        cp -a "$BACKUP_DIR/data" "$PLUGIN_DIR/data"
    fi
else
    mkdir -p "$PLUGIN_DIR"
    unzip -o "$TMP_FILE" -d "$PLUGIN_DIR" -x "data/*" >/dev/null
fi

rm -f "$TMP_FILE"

# 清除 __pycache__ 避免旧字节码缓存
find "$PLUGIN_DIR" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

# 执行插件自带的 install.sh（宝塔插件注册流程）
if [ -f "$PLUGIN_DIR/install.sh" ]; then
    cd "$PLUGIN_DIR" && bash install.sh install 2>/dev/null || true
fi

# 复制图标到面板静态目录（兼容不同版本宝塔的路径）
for ICON_DIR in \
    "$PANEL_DIR/BTPanel/static/images/soft_ico" \
    "$PANEL_DIR/BTPanel/static/img/soft_ico" \
    "$PANEL_DIR/static/images/soft_ico"; do
    mkdir -p "$ICON_DIR" 2>/dev/null || continue
    for ext in png svg; do
        if [ -f "$PLUGIN_DIR/icon.$ext" ]; then
            cp -f "$PLUGIN_DIR/icon.$ext" "$ICON_DIR/ico-sslbt.$ext"
            chmod 644 "$ICON_DIR/ico-sslbt.$ext"
        fi
    done
done

# 注册插件（官方 .pl 文件方式）
touch "$PANEL_DIR/data/sslbt.pl"

# 写入 release_url
DATA_DIR="$PLUGIN_DIR/data"
mkdir -p "$DATA_DIR"
CONFIG_FILE="$DATA_DIR/config.json"

if [ -f "$CONFIG_FILE" ]; then
    python3 -c "
import json, os, tempfile
path = '$CONFIG_FILE'
url = '$RELEASE_URL'
try:
    with open(path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
except (json.JSONDecodeError, FileNotFoundError):
    cfg = {}
cfg['release_url'] = url
d = os.path.dirname(path) or '.'
fd, tmp = tempfile.mkstemp(dir=d)
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
os.replace(tmp, path)
os.chmod(path, 0o600)
" 2>/dev/null || echo_warn "写入 release_url 失败"
else
    printf '{\n  "release_url": "%s"\n}\n' "$RELEASE_URL" > "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
fi

echo ""
echo_info "安装完成！$VERSION"
echo ""
echo "插件位置: $PLUGIN_DIR"
echo "打开宝塔面板 → 软件商店 → 第三方应用 → sslbt 证书管理"
echo "刷新面板页面即可使用，无需重启面板。"
