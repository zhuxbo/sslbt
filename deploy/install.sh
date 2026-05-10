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
            echo "  [host]         发布服务器域名（默认 release.cnssl.com）"
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

# 检测 Python3 路径（宝塔自带 Python 不在系统 PATH 中，需查找已知位置）
PYTHON3=""
for _py in python3 /www/server/panel/pyenv/bin/python3; do
    if command -v "$_py" &>/dev/null && "$_py" -c "import json" &>/dev/null; then
        PYTHON3="$_py"
        break
    fi
done
[ -z "$PYTHON3" ] && { echo_error "未找到 Python3（宝塔环境应内置 /www/server/panel/pyenv/bin/python3）"; exit 1; }

RELEASE_HOST="${RELEASE_HOST:-release.cnssl.com}"
RELEASE_HOST="${RELEASE_HOST%/}"

# 发布目录探测
probe_release_url() {
    local host="$1"
    local candidate body
    for suffix in "/sslbt" "/release/sslbt"; do
        candidate="https://${host}${suffix}"
        body=$(curl -s --connect-timeout 5 --max-time 10 "${candidate}/releases.json" 2>/dev/null || echo "")
        if echo "$body" | grep -q '"latest"'; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

echo_info "探测发布目录..."
RELEASE_URL=$(probe_release_url "$RELEASE_HOST")
if [ -z "$RELEASE_URL" ]; then
    echo_error "发布目录不可达: https://${RELEASE_HOST}/sslbt/releases.json 与 https://${RELEASE_HOST}/release/sslbt/releases.json 均无响应"
    exit 1
fi

[ "$EUID" -ne 0 ] && { echo_error "请使用 root 权限运行"; exit 1; }

PANEL_DIR="/www/server/panel"
PLUGIN_DIR="$PANEL_DIR/plugin/sslbt"
[ ! -d "$PANEL_DIR" ] && { echo_error "未检测到宝塔面板: $PANEL_DIR"; exit 1; }
echo_info "宝塔面板: $PANEL_DIR"
echo_info "使用发布地址: $RELEASE_URL"

normalize_version() {
    local ver="$1"
    [[ "$ver" != v* ]] && echo "v$ver" || echo "$ver"
}

# 下载 releases.json（缓存复用）
echo_info "获取版本信息..."
RELEASES_JSON=$(curl -s --connect-timeout 10 --max-filesize 262144 "$RELEASE_URL/releases.json" 2>/dev/null)

get_target_version() {
    if [ -n "$TARGET_VERSION" ]; then
        [ -z "$CHANNEL" ] && { [[ "$TARGET_VERSION" == *"-"* ]] && CHANNEL="dev" || CHANNEL="main"; }
        normalize_version "$TARGET_VERSION"
        return
    fi

    [ -z "$RELEASES_JSON" ] && { echo ""; return; }
    [ -z "$CHANNEL" ] && CHANNEL="main"

    # spec 6.1: 通道做顶层 key，读取 [channel].latest
    local version
    version=$($PYTHON3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('$CHANNEL', {}).get('latest', ''))
except: pass
" <<< "$RELEASES_JSON" 2>/dev/null)
    [ -n "$version" ] && normalize_version "$version" || echo ""
}

VERSION=$(get_target_version)
[ -z "$VERSION" ] && { echo_error "无法获取版本信息: $RELEASE_URL/releases.json"; exit 1; }
[ -z "$CHANNEL" ] && { [[ "$VERSION" == *"-"* ]] && CHANNEL="dev" || CHANNEL="main"; }
[ "$CHANNEL" = "dev" ] && echo_info "目标版本: $VERSION (测试版)" || echo_info "目标版本: $VERSION (稳定版)"

# 检测已安装版本
CURRENT_VERSION=""
if [ -f "$PLUGIN_DIR/info.json" ]; then
    CURRENT_VERSION=$($PYTHON3 -c "
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

# spec 6.3: GET {release_url}/{channel}/v{version}/{filename}
DOWNLOAD_URL="$RELEASE_URL/$CHANNEL/$VERSION/sslbt.zip"
TMP_FILE="/tmp/sslbt-$VERSION.zip"

echo_info "下载 sslbt.zip..."
if ! curl -fsSL --connect-timeout 30 --max-filesize 10485760 "$DOWNLOAD_URL" -o "$TMP_FILE" 2>/dev/null; then
    echo_error "下载失败: $DOWNLOAD_URL"
    rm -f "$TMP_FILE"
    exit 1
fi

# SHA256 校验（spec 6.1: checksums 按文件名索引）
EXPECTED_HASH=""
if [ -n "$RELEASES_JSON" ]; then
    # spec 6.1: checksums 内嵌在版本条目中，version 不带 v 前缀
    EXPECTED_HASH=$($PYTHON3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ch = d.get('$CHANNEL', {})
    target = '${VERSION#v}'
    for v in ch.get('versions', []):
        if v.get('version', '') == target:
            h = v.get('checksums', {}).get('sslbt.zip', '')
            if h.startswith('sha256:'):
                print(h[7:])
            break
except: pass
" <<< "$RELEASES_JSON" 2>/dev/null)
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

# spec 10.2: 符号链接防护 — 拒绝包含符号链接的 ZIP
if ! $PYTHON3 -c "
import zipfile, sys
with zipfile.ZipFile(sys.argv[1], 'r') as zf:
    for info in zf.infolist():
        if info.external_attr >> 28 == 0xA:
            sys.stderr.write('symlink: ' + info.filename + '\n')
            sys.exit(1)
" "$TMP_FILE" 2>/dev/null; then
    echo_error "ZIP 包含符号链接，拒绝安装"
    rm -f "$TMP_FILE"
    exit 1
fi

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
    unzip -o "$TMP_FILE" -d "$PLUGIN_DIR" -x "data/*" >/dev/null 2>&1
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
    for ext in png; do
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
    $PYTHON3 -c "
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
