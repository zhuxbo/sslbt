#!/bin/bash
# sslbt 宝塔插件远程发布脚本
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$SCRIPT_DIR/release.conf"
DIST_DIR="$PROJECT_ROOT/dist"

KEEP_VERSIONS=5
SSH_TIMEOUT=10

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()    { echo -e "\n${GREEN}==>${NC} $1"; }

load_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        log_error "配置文件不存在: $CONFIG_FILE"
        log_info "  cp $SCRIPT_DIR/release.conf.example $CONFIG_FILE"
        exit 1
    fi
    source "$CONFIG_FILE"
    if [ ${#SERVERS[@]} -eq 0 ]; then log_error "未配置 SERVERS"; exit 1; fi
    if [ -z "$SSH_USER" ]; then log_error "未配置 SSH_USER"; exit 1; fi
    if [ -z "$SSH_KEY" ]; then log_error "未配置 SSH_KEY"; exit 1; fi
    SSH_KEY="${SSH_KEY/#\~/$HOME}"
    if [ ! -f "$SSH_KEY" ]; then log_error "SSH 密钥不存在: $SSH_KEY"; exit 1; fi
}

parse_server() {
    local server_str="$1"
    IFS=',' read -r SERVER_NAME SERVER_HOST SERVER_PORT SERVER_DIR SERVER_URL <<< "$server_str"
    SERVER_PORT=${SERVER_PORT:-22}
}

ssh_cmd() {
    local host="$1" port="$2"; shift 2
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=$SSH_TIMEOUT \
        -p "$port" "$SSH_USER@$host" "$@"
}

rsync_cmd() {
    local src="$1" host="$2" port="$3" dest="$4"
    rsync -avz --progress -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -p $port" \
        "$src" "$SSH_USER@$host:$dest"
}

get_channel() {
    [[ "$1" == *"-"* ]] && echo "dev" || echo "main"
}

check_tag() {
    local tag="$1"
    local head_commit=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
    local tag_commit=$(git -C "$PROJECT_ROOT" rev-parse "refs/tags/$tag" 2>/dev/null || echo "")
    if [ -z "$tag_commit" ]; then
        log_warning "tag $tag 不存在，建议: git tag $tag && git push origin $tag"
    elif [ "$tag_commit" != "$head_commit" ]; then
        log_warning "tag $tag 指向其他提交，非当前 HEAD"
    else
        log_info "tag $tag 已指向当前提交"
    fi
}

test_ssh_connection() {
    parse_server "$1"
    log_info "测试连接: $SERVER_NAME ($SERVER_HOST:$SERVER_PORT)"
    if ssh_cmd "$SERVER_HOST" "$SERVER_PORT" "echo 'OK'" 2>/dev/null; then
        log_success "$SERVER_NAME: 连接成功"
    else
        log_error "$SERVER_NAME: 连接失败"; return 1
    fi
}

test_all_connections() {
    log_step "测试所有服务器连接..."
    local failed=0
    for server in "${SERVERS[@]}"; do
        test_ssh_connection "$server" || failed=$((failed + 1))
    done
    [ $failed -gt 0 ] && { log_error "$failed 个服务器连接失败"; return 1; }
    log_success "所有服务器连接正常"
}

compute_checksum() {
    CHECKSUM_VALUE="sha256:$(shasum -a 256 "$DIST_DIR/sslbt.zip" | cut -d' ' -f1)"
}

update_releases_json_remote() {
    local server_str="$1" version="$2" channel="$3" checksum="$4"
    parse_server "$server_str"
    log_info "更新 releases.json..."

    ssh_cmd "$SERVER_HOST" "$SERVER_PORT" "RELEASES_FILE='$SERVER_DIR/releases.json' VERSION='$version' CHANNEL='$channel' CHECKSUM='$checksum' python3 << 'PYEOF'
import json, os
from datetime import datetime

releases_file = os.environ['RELEASES_FILE']
version = os.environ['VERSION']
channel = os.environ['CHANNEL']
checksum = os.environ.get('CHECKSUM', '')

# spec 6.1: version 不带 v 前缀
bare = version[1:] if version.startswith('v') else version

def ver_key(s):
    base, _, pre = s.partition('-')
    nums = tuple(int(x) for x in base.split('.'))
    return (nums, 0 if not pre else -1, pre)

# spec 6.1: 通道做顶层 key
data = {}
if os.path.exists(releases_file):
    try:
        with open(releases_file, 'r') as f:
            data = json.load(f)
    except:
        pass

# 清除旧格式遗留字段
for old_key in ('channels', 'versions', 'latest_main', 'latest_dev'):
    data.pop(old_key, None)

if channel not in data:
    data[channel] = {'latest': '', 'versions': []}

ch = data[channel]
versions = ch.get('versions', [])

# spec 6.1: checksums 内嵌在版本条目中
version_entry = {
    'version': bare,
    'released_at': datetime.now().strftime('%Y-%m-%d'),
    'checksums': {'sslbt.zip': checksum},
}

existing = [i for i, v in enumerate(versions) if v['version'] == bare]
if existing:
    versions[existing[0]] = version_entry
else:
    versions.append(version_entry)

versions.sort(key=lambda v: ver_key(v['version']), reverse=True)

ch['latest'] = versions[0]['version'] if versions else ''
ch['versions'] = versions

with open(releases_file, 'w') as f:
    json.dump(data, f, indent=2)
os.chmod(releases_file, 0o644)
print(f'已更新 releases.json: {channel}/{bare}')
PYEOF"
}

cleanup_old_versions_remote() {
    local server_str="$1" channel="$2"
    parse_server "$server_str"
    log_info "清理旧版本（保留 $KEEP_VERSIONS 个）..."

    ssh_cmd "$SERVER_HOST" "$SERVER_PORT" "
        cd \"$SERVER_DIR/$channel\" 2>/dev/null || exit 0
        removed=\$(ls -dt v* 2>/dev/null | tail -n +$((KEEP_VERSIONS + 1)))
        if [ -n \"\$removed\" ]; then
            echo \"\$removed\" | xargs -r rm -rf
        fi
    "

    # 同步 releases.json：移除已删除目录对应的版本条目
    ssh_cmd "$SERVER_HOST" "$SERVER_PORT" "python3 << 'PYEOF'
import json, os
releases_file = '$SERVER_DIR/releases.json'
channel = '$channel'
channel_dir = '$SERVER_DIR/$channel'
if not os.path.exists(releases_file): exit(0)
with open(releases_file, 'r') as f:
    data = json.load(f)
if channel not in data: exit(0)

# spec 6.3: 目录名加 v 前缀，version 字段不带 v 前缀
existing = set()
if os.path.isdir(channel_dir):
    for d in os.listdir(channel_dir):
        if d.startswith('v'):
            existing.add(d[1:])  # 去 v 前缀，与 version 字段一致

versions = data[channel].get('versions', [])
data[channel]['versions'] = [v for v in versions if v['version'] in existing]

def ver_key(s):
    base, _, pre = s.partition('-')
    nums = tuple(int(x) for x in base.split('.'))
    return (nums, 0 if not pre else -1, pre)

filtered = data[channel]['versions']
filtered.sort(key=lambda v: ver_key(v['version']), reverse=True)
data[channel]['latest'] = filtered[0]['version'] if filtered else ''

with open(releases_file, 'w') as f:
    json.dump(data, f, indent=2)
os.chmod(releases_file, 0o644)
PYEOF"
}

upload_to_server() {
    local server_str="$1" version="$2" channel="$3"
    parse_server "$server_str"
    log_step "部署到 $SERVER_NAME ($SERVER_HOST)..."

    local remote_version_dir="$SERVER_DIR/$channel/$version"
    ssh_cmd "$SERVER_HOST" "$SERVER_PORT" "mkdir -p \"$remote_version_dir\""

    log_info "上传 sslbt.zip..."
    rsync_cmd "$DIST_DIR/sslbt.zip" "$SERVER_HOST" "$SERVER_PORT" "$remote_version_dir/"

    log_info "上传 install.sh..."
    rsync_cmd "$PROJECT_ROOT/deploy/install.sh" "$SERVER_HOST" "$SERVER_PORT" "$SERVER_DIR/install.sh"

    update_releases_json_remote "$server_str" "$version" "$channel" "$CHECKSUM_VALUE"

    ssh_cmd "$SERVER_HOST" "$SERVER_PORT" "chmod 644 \"$SERVER_DIR/releases.json\" \"$SERVER_DIR/install.sh\" 2>/dev/null; chmod 644 \"$remote_version_dir/sslbt.zip\" 2>/dev/null"

    cleanup_old_versions_remote "$server_str" "$channel"
    log_success "$SERVER_NAME: 部署完成"
}

show_help() {
    cat << EOF
用法: $0 [选项] <版本号>

选项:
  --test            测试所有服务器 SSH 连接
  --server NAME     只部署到指定服务器
  --upload-only     只上传，跳过构建
  -h, --help        显示帮助

示例:
  $0 0.0.1-beta         发布测试版
  $0 1.0.0              发布正式版
  $0 --server cn 1.0.0  只发布到 cn
  $0 --test             测试连接
EOF
}

main() {
    local version="" target_server="" upload_only=false test_only=false

    while [ $# -gt 0 ]; do
        case "$1" in
            --test)        test_only=true; shift ;;
            --server)      target_server="$2"; shift 2 ;;
            --upload-only) upload_only=true; shift ;;
            -h|--help)     show_help; exit 0 ;;
            -*)            log_error "未知选项: $1"; show_help; exit 1 ;;
            *)             version="$1"; shift ;;
        esac
    done

    echo ""
    echo "========================================"
    echo "  sslbt 宝塔插件远程发布"
    echo "========================================"
    echo ""

    load_config

    if [ "$test_only" = true ]; then
        test_all_connections; exit $?
    fi

    if [ -z "$version" ]; then log_error "必须指定版本号"; exit 1; fi

    local channel=$(get_channel "$version")

    if [ "$channel" = "main" ]; then
        check_tag "v${version#v}"
    fi

    if [[ "$version" != v* ]]; then version="v$version"; fi

    log_info "版本号: $version"
    log_info "发布通道: $channel"
    log_info "目标服务器: ${target_server:-全部}"

    if ! test_all_connections; then
        log_error "请先解决连接问题"; exit 1
    fi

    if [ "$upload_only" = false ]; then
        log_step "运行构建..."
        "$PROJECT_ROOT/scripts/build.sh" "$version"
    fi

    if [ ! -f "$DIST_DIR/sslbt.zip" ]; then log_error "构建产物不存在"; exit 1; fi

    log_step "计算校验和..."
    compute_checksum
    log_info "SHA256: $CHECKSUM_VALUE"

    local success=0 failed=0
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        if [ -n "$target_server" ] && [ "$SERVER_NAME" != "$target_server" ]; then continue; fi
        if upload_to_server "$server" "$version" "$channel"; then
            success=$((success + 1))
        else
            failed=$((failed + 1))
            log_error "$SERVER_NAME: 部署失败"
        fi
    done

    echo ""
    log_step "部署结果"
    log_info "成功: $success 个服务器"
    [ $failed -gt 0 ] && log_error "失败: $failed 个服务器"

    if [ $failed -eq 0 ]; then
        log_success "发布完成！"
        echo ""
        log_info "验证:"
        for server in "${SERVERS[@]}"; do
            parse_server "$server"
            if [ -z "$target_server" ] || [ "$SERVER_NAME" = "$target_server" ]; then
                echo "  curl $SERVER_URL/releases.json | jq ."
            fi
        done
    fi

    return $failed
}

main "$@"
