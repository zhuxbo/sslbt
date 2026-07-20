#!/usr/bin/env bash
# 确定性构建 sslbt 宝塔插件 ZIP 包。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SRC_DIR="$PROJECT_DIR/src"
VERSION="${1:?用法: build.sh <版本号> [输出文件]}"
VERSION="${VERSION#v}"
OUTPUT_FILE="${2:-$PROJECT_DIR/dist/sslbt.zip}"

if ! [[ "$VERSION" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]; then
    echo "错误: 版本号必须是完整 SemVer" >&2
    exit 1
fi

for required in sslbt_main.py index.html info.json install.sh lib; do
    if [ ! -e "$SRC_DIR/$required" ]; then
        echo "错误: 缺少 src/$required" >&2
        exit 1
    fi
done

mkdir -p "$(dirname "$OUTPUT_FILE")"
rm -f "$OUTPUT_FILE"

BUILD_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$PROJECT_DIR" show -s --format=%ct HEAD 2>/dev/null || date +%s)}"

python3 - "$SRC_DIR" "$OUTPUT_FILE" "$VERSION" "$BUILD_EPOCH" <<'PY'
import json
import os
import stat
import sys
import zipfile
from datetime import datetime, timezone

source, output, version, epoch = sys.argv[1:]
timestamp = datetime.fromtimestamp(max(int(epoch), 315532800), timezone.utc)
zip_time = (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second)

files = []
for root, dirs, names in os.walk(source):
    links = [name for name in dirs + names if os.path.islink(os.path.join(root, name))]
    if links:
        raise SystemExit('错误: src/ 禁止包含符号链接: ' + os.path.join(root, links[0]))
    dirs[:] = sorted(d for d in dirs if d != '__pycache__' and d != 'data')
    for name in sorted(names):
        if name.endswith('.pyc') or name == '.DS_Store':
            continue
        path = os.path.join(root, name)
        files.append((os.path.relpath(path, source).replace(os.sep, '/'), path))

with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for relative, path in sorted(files):
        with open(path, 'rb') as handle:
            content = handle.read()
        if relative == 'info.json':
            info = json.loads(content.decode('utf-8'))
            info['versions'] = version
            content = (json.dumps(info, indent=4, ensure_ascii=False) + '\n').encode('utf-8')
        entry = zipfile.ZipInfo(relative, zip_time)
        mode = 0o755 if relative.endswith('.sh') else 0o644
        entry.external_attr = (stat.S_IFREG | mode) << 16
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.create_system = 3
        archive.writestr(entry, content)
PY

echo "构建完成: $OUTPUT_FILE (v$VERSION)"
