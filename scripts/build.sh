#!/bin/bash
# 构建 sslbt 宝塔插件 ZIP 包
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$PROJECT_DIR/src"
DIST_DIR="$PROJECT_DIR/dist"

# 版本号（必传参数）
VERSION="${1:?用法: build.sh <版本号>}"

# normalize: 确保 v 前缀
[[ "$VERSION" != v* ]] && VERSION="v$VERSION"

echo "构建 sslbt 宝塔插件 $VERSION"

# 清理
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# 创建临时副本（不修改源文件）
TMP_DIR=$(mktemp -d)
trap "rm -rf '$TMP_DIR'" EXIT

cp -a "$SRC_DIR/." "$TMP_DIR/"

# 清理 __pycache__
find "$TMP_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find "$TMP_DIR" -name '*.pyc' -delete 2>/dev/null || true

# 注入版本号到临时副本的 info.json
INFO_VERSION="${VERSION#v}"
python3 -c "
import json
path = '$TMP_DIR/info.json'
with open(path, 'r') as f:
    info = json.load(f)
info['versions'] = '$INFO_VERSION'
with open(path, 'w') as f:
    json.dump(info, f, indent=4, ensure_ascii=False)
"

# 检查必要文件
for f in sslbt_main.py index.html info.json install.sh; do
    if [ ! -f "$TMP_DIR/$f" ]; then
        echo "错误: 缺少文件 $f"
        exit 1
    fi
done

if [ ! -d "$TMP_DIR/lib" ]; then
    echo "错误: 缺少 lib/ 目录"
    exit 1
fi

# 构建 ZIP
cd "$TMP_DIR"
zip -r "$DIST_DIR/sslbt.zip" . \
    -x "__pycache__/*" \
    -x "*.pyc" \
    -x ".DS_Store" \
    -x "data/*"

echo ""
echo "构建完成: dist/sslbt.zip ($VERSION)"
ls -lh "$DIST_DIR/sslbt.zip"
