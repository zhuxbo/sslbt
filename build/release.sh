#!/usr/bin/env bash
# sslbt 发布资产与多节点事务原语；Git/PR/GitHub Release 编排见 skills/remote-release.md。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MANIFEST_TOOL="$PROJECT_ROOT/scripts/release_manifest.py"
CONFIG_FILE="${SSLBT_RELEASE_CONFIG:-$SCRIPT_DIR/release.conf}"
KEEP_VERSIONS=5
SSH_TIMEOUT=10
MODE=""
VERSION=""
BUNDLE_DIR=""
LOCKS_HELD=false
LOCAL_LOCK_HELD=false
LOCK_OWNER=""
LOCAL_LOCK_DIR=""

file_mode() {
    python3 -c "import os, sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])" "$1"
}

sha256_stdin() {
    python3 -c "import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"
}

die() {
    echo "错误: $*" >&2
    if [ "${LOCKS_HELD:-false}" = true ]; then
        release_publish_locks || true
    fi
    release_local_lock || true
    exit 1
}
info() { echo "[INFO] $*"; }

usage() {
    cat <<'EOF'
用法:
  build/release.sh --prepare VERSION [--bundle DIR]
  build/release.sh --stage BUNDLE_DIR
  build/release.sh --publish BUNDLE_DIR
  build/release.sh --verify BUNDLE_DIR
  build/release.sh --resume BUNDLE_DIR
  build/release.sh --dev VERSION [--bundle DIR]
  build/release.sh VERSION [--bundle DIR]    # 兼容旧入口，仅限预发布版
  build/release.sh --dry-run VERSION
  build/release.sh --test-connections

--prepare 只构建一次并持久化 manifest；--stage/--publish/--verify 不构建。
main 在 tag 创建后只能使用 --publish/--resume 读取已有 bundle，禁止重建。
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prepare|--dev|--dry-run)
            [ -z "$MODE" ] || die "只能选择一个模式"
            MODE="${1#--}"; VERSION="${2:-}"; shift 2 ;;
        --stage|--publish|--verify|--resume)
            [ -z "$MODE" ] || die "只能选择一个模式"
            MODE="${1#--}"; BUNDLE_DIR="${2:-}"; shift 2 ;;
        --test-connections)
            [ -z "$MODE" ] || die "只能选择一个模式"
            MODE="test-connections"; shift ;;
        --bundle) BUNDLE_DIR="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "未知参数: $1" ;;
        *)
            [ -z "$MODE" ] || die "只能选择一个模式"
            MODE="dev"; VERSION="$1"; shift ;;
    esac
done
[ -n "$MODE" ] || { usage; exit 1; }

normalize_version() {
    VERSION="${1#v}"
    python3 - "$MANIFEST_TOOL" "$VERSION" <<'PY'
import importlib.util, pathlib, sys
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('release_manifest', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.parse_version(sys.argv[2])
PY
}

manifest_value() {
    python3 - "$BUNDLE_DIR/manifest.json" "$1" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as handle:
    value = json.load(handle)
for part in sys.argv[2].split('.'):
    value = value[int(part)] if part.isdigit() else value[part]
print(str(value).lower() if isinstance(value, bool) else value)
PY
}

verify_bundle() {
    [ -f "$BUNDLE_DIR/manifest.json" ] || die "bundle 缺少 manifest.json: $BUNDLE_DIR"
    python3 "$MANIFEST_TOOL" verify --manifest "$BUNDLE_DIR/manifest.json" --bundle-dir "$BUNDLE_DIR"
    VERSION="$(manifest_value version)"
    CHANNEL="$(manifest_value channel)"
    SOURCE_COMMIT="$(manifest_value source_commit)"
    ASSET_SHA="$(manifest_value assets.0.sha256)"
    INSTALLER_SHA="$(manifest_value bootstrap.sha256)"
    TXN_ID="sslbt-v${VERSION}-${SOURCE_COMMIT:0:12}-${ASSET_SHA:0:8}-${INSTALLER_SHA:0:8}"
}

canonical_index_hash() {
    local canonical
    canonical="$(python3 "$MANIFEST_TOOL" canonical --index "$1")"
    printf '%s' "$canonical" | sha256_stdin
}

seal_transaction() {
    local expected="$BUNDLE_DIR/release-candidate.expected.json"
    python3 "$MANIFEST_TOOL" index --base "$BUNDLE_DIR/releases-baseline.json" \
        --manifest "$BUNDLE_DIR/manifest.json" --bundle-dir "$BUNDLE_DIR" \
        --keep "${KEEP_VERSIONS:-5}" --output "$expected"
    cmp "$expected" "$BUNDLE_DIR/release-candidate.json" || die "候选索引不是由原始基线和 manifest 唯一生成"
    rm -f "$expected"
    python3 - "$BUNDLE_DIR" <<'PY'
import hashlib, json, os, sys
root = sys.argv[1]
names = ('manifest.json', 'releases-baseline.json', 'release-candidate.json',
         'assets/sslbt.zip', 'bootstrap/install.sh')
files = {}
for name in names:
    path = os.path.join(root, name)
    with open(path, 'rb') as handle:
        files[name] = hashlib.sha256(handle.read()).hexdigest()
data = {'schema_version': 1, 'files': files}
path = os.path.join(root, 'transaction.json')
with open(path + '.tmp', 'w', encoding='utf-8') as handle:
    json.dump(data, handle, indent=2, sort_keys=True)
    handle.write('\n')
os.replace(path + '.tmp', path)
os.chmod(path, 0o400)
PY
}

verify_transaction() {
    [ -f "$BUNDLE_DIR/transaction.json" ] || die "bundle 缺少 stage 事务封存 transaction.json"
    python3 - "$BUNDLE_DIR" <<'PY'
import hashlib, json, os, sys
root = sys.argv[1]
with open(os.path.join(root, 'transaction.json'), encoding='utf-8') as handle:
    transaction = json.load(handle)
expected = ('manifest.json', 'releases-baseline.json', 'release-candidate.json',
            'assets/sslbt.zip', 'bootstrap/install.sh')
if transaction.get('schema_version') != 1 or set(transaction.get('files', {})) != set(expected):
    raise SystemExit('事务封存结构无效')
for name in expected:
    with open(os.path.join(root, name), 'rb') as handle:
        actual = hashlib.sha256(handle.read()).hexdigest()
    if actual != transaction['files'][name]:
        raise SystemExit('事务封存哈希不匹配: ' + name)
PY
    BASELINE_HASH="$(canonical_index_hash "$BUNDLE_DIR/releases-baseline.json")"
    CANDIDATE_HASH="$(canonical_index_hash "$BUNDLE_DIR/release-candidate.json")"
}

prepare_bundle() {
    normalize_version "$1"
    local channel="main" dirty=false
    [[ "$VERSION" == *-* ]] && channel="dev"
    [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ] && dirty=true
    local ignored_inputs
    ignored_inputs="$(git -C "$PROJECT_ROOT" ls-files --others --ignored --exclude-standard -- src | \
        python3 -c "import sys; print(''.join(p for p in sys.stdin if '/__pycache__/' not in p and '/data/' not in p and not p.rstrip().endswith(('.pyc', '/.DS_Store'))), end='')")"
    [ -z "$ignored_inputs" ] || die "src/ 存在会进入构建但不受 Git 跟踪的忽略文件: $ignored_inputs"
    local commit build_epoch manifest_epoch
    commit="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
    build_epoch="$(git -C "$PROJECT_ROOT" show -s --format=%ct HEAD)"
    manifest_epoch="$(date -u +%s)"

    if [ "$channel" = main ]; then
        [ "$dirty" = false ] || die "main 正式构建要求工作区干净"
        [ "$(git -C "$PROJECT_ROOT" branch --show-current)" = main ] || die "main 正式构建必须位于 main 分支"
        [ "$commit" = "$(git -C "$PROJECT_ROOT" rev-parse origin/main)" ] || die "本地 main 必须与 origin/main 一致"
        if git -C "$PROJECT_ROOT" show-ref --verify --quiet "refs/tags/v$VERSION"; then
            die "版本 tag 已存在；tag 创建后禁止重建，只能 --resume"
        fi
    fi

    if [ -z "$BUNDLE_DIR" ]; then
        if [ "$channel" = main ]; then
            BUNDLE_DIR="$PROJECT_ROOT/.release-bundles/main/v$VERSION"
        else
            BUNDLE_DIR="$PROJECT_ROOT/.release-bundles/dev/v$VERSION-$(date -u +%Y%m%dT%H%M%SZ)-$$"
        fi
    fi
    if [ -e "$BUNDLE_DIR" ]; then
        die "bundle 目录已存在，拒绝覆盖: $BUNDLE_DIR"
    fi
    mkdir -p "$BUNDLE_DIR/assets" "$BUNDLE_DIR/bootstrap"
    SOURCE_DATE_EPOCH="$build_epoch" "$PROJECT_ROOT/scripts/build.sh" "$VERSION" "$BUNDLE_DIR/assets/sslbt.zip"
    cp "$PROJECT_ROOT/deploy/install.sh" "$BUNDLE_DIR/bootstrap/install.sh"
    local manifest_args=(create --version "$VERSION" --channel "$channel" --commit "$commit" --epoch "$manifest_epoch")
    [ "$dirty" = true ] && manifest_args+=(--dirty)
    manifest_args+=(--asset "$BUNDLE_DIR/assets/sslbt.zip"
        --bootstrap "$BUNDLE_DIR/bootstrap/install.sh" --output "$BUNDLE_DIR/manifest.json")
    python3 "$MANIFEST_TOOL" "${manifest_args[@]}"
    verify_bundle
    info "bundle 已持久化: $BUNDLE_DIR"
}

load_config() {
    [ -f "$CONFIG_FILE" ] || die "缺少 $CONFIG_FILE"
    local config_mode
    config_mode="$(file_mode "$CONFIG_FILE")"
    [ "$config_mode" = 600 ] || die "发布配置权限必须是 600"
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
    [ "${#SERVERS[@]}" -gt 0 ] || die "未配置 SERVERS"
    [ -n "${SSH_USER:-}" ] || die "未配置 SSH_USER"
    [ -n "${SSH_KEY:-}" ] || die "未配置 SSH_KEY"
    [[ "$SSH_USER" =~ ^[A-Za-z0-9._-]+$ ]] || die "SSH_USER 格式无效"
    SSH_KEY="${SSH_KEY/#\~/$HOME}"
    [ -f "$SSH_KEY" ] || die "SSH 密钥不存在"
    local key_mode
    key_mode="$(file_mode "$SSH_KEY")"
    [ "$key_mode" = 600 ] || die "SSH 密钥权限必须是 600"
}

parse_server() {
    IFS=',' read -r SERVER_NAME SERVER_HOST SERVER_PORT SERVER_DIR <<< "$1"
    SERVER_PORT="${SERVER_PORT:-22}"
    [ -n "$SERVER_NAME" ] && [ -n "$SERVER_HOST" ] && [ -n "$SERVER_DIR" ] || die "SERVERS 条目无效"
    [[ "$SERVER_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "服务器名称格式无效"
    [[ "$SERVER_HOST" =~ ^[A-Za-z0-9._:-]+$ ]] || die "服务器主机格式无效"
    [[ "$SERVER_PORT" =~ ^[0-9]+$ ]] || die "服务器端口格式无效"
    [[ "$SERVER_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "服务器目录必须是安全的绝对路径"
    [[ "/$SERVER_DIR/" != *"/../"* ]] || die "服务器目录禁止包含 .."
}

ssh_run() {
    ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -o "ConnectTimeout=$SSH_TIMEOUT" -p "$SERVER_PORT" "$SSH_USER@$SERVER_HOST" "$@"
}

copy_to_server() {
    scp -q -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -P "$SERVER_PORT" "$1" "$SSH_USER@$SERVER_HOST:$2"
}

test_connections() {
    load_config
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        ssh_run true || die "节点连接失败: $SERVER_NAME"
        info "节点可达: $SERVER_NAME"
    done
}

fetch_consistent_index() {
    local base_dir="$BUNDLE_DIR/index-baseline"
    mkdir -p "$base_dir"
    local canonical_hash=""
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        local output="$base_dir/$SERVER_NAME.json"
        ssh_run "if [ -f '$SERVER_DIR/releases.json' ]; then cat '$SERVER_DIR/releases.json'; else printf '{}\n'; fi" \
            > "$output" || die "无法读取 $SERVER_NAME 的 releases.json 基线"
        local canonical current_hash
        canonical="$(python3 "$MANIFEST_TOOL" canonical --index "$output")"
        current_hash="$(printf '%s' "$canonical" | sha256_stdin)"
        if [ -z "$canonical_hash" ]; then
            canonical_hash="$current_hash"
            cp "$output" "$BUNDLE_DIR/releases-baseline.json"
        elif [ "$canonical_hash" != "$current_hash" ]; then
            die "发布节点 releases.json 基线不一致，停止发布"
        fi
    done
    python3 "$MANIFEST_TOOL" index --base "$BUNDLE_DIR/releases-baseline.json" \
        --manifest "$BUNDLE_DIR/manifest.json" --bundle-dir "$BUNDLE_DIR" \
        --keep "${KEEP_VERSIONS:-5}" --output "$BUNDLE_DIR/release-candidate.json"
    python3 "$MANIFEST_TOOL" check-index --index "$BUNDLE_DIR/release-candidate.json" \
        --manifest "$BUNDLE_DIR/manifest.json" --bundle-dir "$BUNDLE_DIR"
    seal_transaction
}

stage_nodes() {
    local resume="${1:-false}"
    verify_bundle
    load_config
    if [ "$resume" = false ]; then
        if [ -f "$BUNDLE_DIR/release-candidate.json" ]; then
            if [ -f "$BUNDLE_DIR/transaction.json" ]; then
                verify_transaction
            else
                git -C "$PROJECT_ROOT" show-ref --verify --quiet "refs/tags/v$VERSION" \
                    && die "tag 已存在但事务未封存，禁止重建恢复证据"
                seal_transaction
                verify_transaction
            fi
            info "复用原候选索引继续 tag 前 stage"
        else
            fetch_consistent_index
            verify_transaction
        fi
    else
        verify_transaction
    fi

    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        local stage="$SERVER_DIR/.staging/$TXN_ID"
        local public="$SERVER_DIR/$CHANNEL/v$VERSION/sslbt.zip"
        if [ "$resume" = false ] && [ "$CHANNEL" = main ]; then
            ssh_run "test ! -e '$SERVER_DIR/$CHANNEL/v$VERSION'" || die "$SERVER_NAME 的 main/v$VERSION 已存在"
        fi
        ssh_run "mkdir -p '$stage'"
        if [ "$CHANNEL" = main ]; then
            copy_to_server "$BUNDLE_DIR/bootstrap/install.sh" "$stage/install.sh"
        fi
        copy_to_server "$BUNDLE_DIR/manifest.json" "$stage/manifest.json"
        copy_to_server "$BUNDLE_DIR/release-candidate.json" "$stage/releases.json"
        if [ "$resume" = true ] && ssh_run "test -f '$public' && test \"\$(sha256sum '$public' | cut -d' ' -f1)\" = '$ASSET_SHA'"; then
            info "$SERVER_NAME 已有一致公开资产"
        else
            copy_to_server "$BUNDLE_DIR/assets/sslbt.zip" "$stage/sslbt.zip"
            ssh_run "test \"\$(sha256sum '$stage/sslbt.zip' | cut -d' ' -f1)\" = '$ASSET_SHA'" \
                || die "$SERVER_NAME 暂存资产哈希错误"
            info "$SERVER_NAME 暂存并校验完成"
        fi
    done
    if [ "$CHANNEL" = main ]; then
        info "所有节点暂存完成；main 通道可继续创建不可变 tag 和 draft GitHub Release"
    else
        info "所有节点暂存完成；dev 通道无需 tag 和 GitHub Release，将继续发布"
    fi
}

require_main_tag() {
    [ "$CHANNEL" != main ] && return
    local tag_commit
    tag_commit="$(git -C "$PROJECT_ROOT" rev-parse "v$VERSION^{commit}" 2>/dev/null || true)"
    [ "$tag_commit" = "$SOURCE_COMMIT" ] || die "main publish/resume 要求 v$VERSION 精确指向 manifest commit"
    local remote_tags remote_commit
    remote_tags="$(git -C "$PROJECT_ROOT" ls-remote origin "refs/tags/v$VERSION" "refs/tags/v$VERSION^{}")" \
        || die "无法核验远端版本 tag"
    remote_commit="$(printf '%s\n' "$remote_tags" | awk -v tag="refs/tags/v$VERSION" '$2 == tag "^{}" {print $1}')"
    [ -n "$remote_commit" ] || remote_commit="$(printf '%s\n' "$remote_tags" | awk -v tag="refs/tags/v$VERSION" '$2 == tag {print $1}')"
    [ "$remote_commit" = "$SOURCE_COMMIT" ] || die "远端 v$VERSION 不存在或未指向 manifest commit"
}

remote_index_hash() {
    local tmp canonical
    tmp="$(mktemp)"
    ssh_run "if [ -f '$SERVER_DIR/releases.json' ]; then cat '$SERVER_DIR/releases.json'; else printf '{}\n'; fi" \
        > "$tmp" || { rm -f "$tmp"; die "无法读取 $SERVER_NAME 的当前 releases.json"; }
    canonical="$(python3 "$MANIFEST_TOOL" canonical --index "$tmp")"
    rm -f "$tmp"
    printf '%s' "$canonical" | sha256_stdin
}

acquire_publish_locks() {
    local server
    acquire_local_lock
    LOCK_OWNER="$TXN_ID:$(hostname):$$:$(date -u +%s):$RANDOM"
    LOCKS_HELD=true
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        local lock="$SERVER_DIR/.publish-lock"
        local reclaim=false
        [ "$MODE" = resume ] && reclaim=true
        ssh_run "if mkdir '$lock' 2>/dev/null; then printf '%s\n' '$LOCK_OWNER' > '$lock/owner'; \
            elif [ '$reclaim' = true ] && grep -q '^$TXN_ID:' '$lock/owner' 2>/dev/null; then \
                rm -rf '$lock' && mkdir '$lock' && printf '%s\n' '$LOCK_OWNER' > '$lock/owner'; \
            else false; fi" \
            || die "$SERVER_NAME 已被其他发布事务锁定"
    done
}

acquire_local_lock() {
    LOCAL_LOCK_DIR="$BUNDLE_DIR/.publish-process.lock"
    if ! mkdir "$LOCAL_LOCK_DIR" 2>/dev/null; then
        die "同一 bundle 已有发布进程或遗留本地锁: $LOCAL_LOCK_DIR"
    fi
    LOCAL_LOCK_HELD=true
    printf '%s:%s\n' "$(hostname)" "$$" > "$LOCAL_LOCK_DIR/owner"
}

release_local_lock() {
    if [ "${LOCAL_LOCK_HELD:-false}" = true ] && [ -n "${LOCAL_LOCK_DIR:-}" ]; then
        rm -rf "$LOCAL_LOCK_DIR"
    fi
    LOCAL_LOCK_HELD=false
}

release_publish_locks() {
    local server
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        local lock="$SERVER_DIR/.publish-lock"
        ssh_run "if test \"\$(cat '$lock/owner' 2>/dev/null)\" = '$LOCK_OWNER'; then rm -rf '$lock'; fi" || true
    done
    LOCKS_HELD=false
    release_local_lock
}

assert_indexes_publishable() {
    local server current
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        current="$(remote_index_hash)"
        if [ "$current" != "$BASELINE_HASH" ] && [ "$current" != "$CANDIDATE_HASH" ]; then
            die "$SERVER_NAME 的索引已在 stage 后变化，拒绝覆盖并发或更新版本"
        fi
    done
}

assert_all_indexes_candidate() {
    local server current
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        current="$(remote_index_hash)"
        [ "$current" = "$CANDIDATE_HASH" ] || die "$SERVER_NAME 的完整索引与封存候选不一致"
    done
}

all_indexes_are_candidate() {
    local server current
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        current="$(remote_index_hash)"
        [ "$current" = "$CANDIDATE_HASH" ] || return 1
    done
}

remote_asset_is_valid() {
    local path="$1"
    ssh_run "test -f '$path' && test \"\$(sha256sum '$path' | cut -d' ' -f1)\" = '$ASSET_SHA'"
}

publish_nodes() {
    verify_bundle
    load_config
    require_main_tag
    verify_transaction
    acquire_publish_locks
    assert_indexes_publishable
    if all_indexes_are_candidate; then
        finish_forward_transaction
        return
    fi
    local published=()
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        local stage="$SERVER_DIR/.staging/$TXN_ID"
        local target="$SERVER_DIR/$CHANNEL/v$VERSION"
        local rollback="$SERVER_DIR/.rollback/$TXN_ID"
        if ! remote_asset_is_valid "$target/sslbt.zip"; then
            ssh_run "test -f '$stage/sslbt.zip' && test \"\$(sha256sum '$stage/sslbt.zip' | cut -d' ' -f1)\" = '$ASSET_SHA'" \
                || die "$SERVER_NAME 未完成暂存"
            if [ "$CHANNEL" = main ]; then
                ssh_run "test ! -e '$target'" || die "$SERVER_NAME 的 main/v$VERSION 已存在，禁止覆盖"
            fi
            if ! ssh_run "rm -rf '$rollback' && mkdir -p '$rollback' '$(dirname "$target")'; \
                if [ -e '$target' ]; then mv '$target' '$rollback/version'; fi; \
                : > '$rollback/asset.changed'; \
                mkdir -p '$target'; cp '$stage/sslbt.zip' '$target/sslbt.zip'; chmod 644 '$target/sslbt.zip'"; then
                rollback_nodes "$server" || true
                [ "${#published[@]}" -eq 0 ] || rollback_nodes "${published[@]}" || true
                die "$SERVER_NAME 公开资产准备失败，已尝试回滚已处理节点"
            fi
        fi
        published+=("$server")
    done

    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        local stage="$SERVER_DIR/.staging/$TXN_ID"
        local rollback="$SERVER_DIR/.rollback/$TXN_ID"
        local installer_publish=""
        if [ "$CHANNEL" = main ]; then
            installer_publish="cp '$stage/install.sh' '$SERVER_DIR/install.sh.tmp'; mv '$SERVER_DIR/install.sh.tmp' '$SERVER_DIR/install.sh'; chmod 644 '$SERVER_DIR/install.sh';"
        fi
        if ! ssh_run "mkdir -p '$rollback'; \
            if [ ! -f '$rollback/releases.json' ] && [ ! -f '$rollback/releases.json.absent' ]; then \
                if [ -f '$SERVER_DIR/releases.json' ]; then cp '$SERVER_DIR/releases.json' '$rollback/releases.json'; else : > '$rollback/releases.json.absent'; fi; \
            fi; \
            if [ '$CHANNEL' = main ] && [ ! -f '$rollback/install.sh' ] && [ ! -f '$rollback/install.sh.absent' ]; then \
                if [ -f '$SERVER_DIR/install.sh' ]; then cp '$SERVER_DIR/install.sh' '$rollback/install.sh'; else : > '$rollback/install.sh.absent'; fi; \
            fi; \
            cp '$stage/releases.json' '$SERVER_DIR/releases.json.tmp'; mv '$SERVER_DIR/releases.json.tmp' '$SERVER_DIR/releases.json'; \
            chmod 644 '$SERVER_DIR/releases.json'; $installer_publish"; then
            rollback_nodes "${published[@]}" || true
            die "$SERVER_NAME 公开索引失败，已尝试回滚全部节点"
        fi
    done
    for server in "${SERVERS[@]}"; do
        if ! verify_one_node "$server"; then
            rollback_nodes "${published[@]}" || true
            die "全节点验收失败，已尝试回滚公开索引和资产"
        fi
    done
    # 刚以「失败即回滚」语义全节点验收过，收尾无需重复验收（其间无任何远端写、锁仍持有）
    finish_forward_transaction verified
}

rollback_nodes() {
    local server failed=false
    for server in "$@"; do
        parse_server "$server"
        local target="$SERVER_DIR/$CHANNEL/v$VERSION"
        local rollback="$SERVER_DIR/.rollback/$TXN_ID"
        if ! ssh_run "if [ -f '$rollback/releases.json' ]; then \
                cp '$rollback/releases.json' '$SERVER_DIR/releases.json.rollback.tmp' && \
                mv '$SERVER_DIR/releases.json.rollback.tmp' '$SERVER_DIR/releases.json'; \
            elif [ -f '$rollback/releases.json.absent' ]; then rm -f '$SERVER_DIR/releases.json'; fi; \
            if [ -f '$rollback/install.sh' ]; then \
                cp '$rollback/install.sh' '$SERVER_DIR/install.sh.rollback.tmp' && \
                mv '$SERVER_DIR/install.sh.rollback.tmp' '$SERVER_DIR/install.sh'; \
            elif [ -f '$rollback/install.sh.absent' ]; then rm -f '$SERVER_DIR/install.sh'; fi; \
            if [ -f '$rollback/asset.changed' ]; then \
                rm -rf '$target'; if [ -d '$rollback/version' ]; then mv '$rollback/version' '$target'; fi; \
            fi"; then
            echo "ERROR: $SERVER_NAME 回滚失败，保留事务证据" >&2
            failed=true
        fi
    done
    [ "$failed" = false ]
}

finish_forward_transaction() {
    # $1 = verified 表示调用方刚完成全节点验收，跳过重复验收；
    # 从「索引已是候选」快路径（含 --resume 中断恢复）进入时无人验收过，必须自行验收
    local server verified="${1:-}"
    if [ "$verified" != verified ]; then
        for server in "${SERVERS[@]}"; do
            verify_one_node "$server" || die "forward-only 全节点验收失败；保留原事务继续恢复"
        done
    fi
    assert_all_indexes_candidate
    for server in "${SERVERS[@]}"; do
        cleanup_one_node "$server" || die "旧版本清理失败；保留 bundle 后重新验收"
    done
    assert_all_indexes_candidate
    for server in "${SERVERS[@]}"; do
        parse_server "$server"
        ssh_run "rm -rf '$SERVER_DIR/.staging/$TXN_ID' '$SERVER_DIR/.rollback/$TXN_ID'" \
            || die "$SERVER_NAME 事务临时目录清理失败"
    done
    release_publish_locks
    info "所有发布节点已公开并完成对账"
}

verify_nodes() {
    verify_bundle
    load_config
    for server in "${SERVERS[@]}"; do
        verify_one_node "$server" || die "节点验收失败"
    done
}

verify_one_node() {
    parse_server "$1"
    local tmp
    tmp="$(mktemp)"
    if ! remote_asset_is_valid "$SERVER_DIR/$CHANNEL/v$VERSION/sslbt.zip"; then
        rm -f "$tmp"
        return 1
    fi
    if ! ssh_run "cat '$SERVER_DIR/releases.json'" > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    if [ "$CHANNEL" = main ]; then
        if ! ssh_run "test -f '$SERVER_DIR/install.sh' && test \"\$(sha256sum '$SERVER_DIR/install.sh' | cut -d' ' -f1)\" = '$INSTALLER_SHA'"; then
            rm -f "$tmp"
            return 1
        fi
    fi
    if ! python3 "$MANIFEST_TOOL" check-index --index "$tmp" --manifest "$BUNDLE_DIR/manifest.json" --bundle-dir "$BUNDLE_DIR"; then
        rm -f "$tmp"
        return 1
    fi
    rm -f "$tmp"
    info "$SERVER_NAME 资产与索引一致"
}

cleanup_one_node() {
    parse_server "$1"
    ssh_run "SERVER_DIR='$SERVER_DIR' CHANNEL='$CHANNEL' python3 - <<'PY'
import json, os, re, shutil
root = os.environ['SERVER_DIR']
channel = os.environ['CHANNEL']
with open(os.path.join(root, 'releases.json'), encoding='utf-8') as handle:
    data = json.load(handle)
allowed = {'v' + entry['version'] for entry in data[channel]['versions']}
channel_dir = os.path.join(root, channel)
if os.path.isdir(channel_dir):
    for name in os.listdir(channel_dir):
        if re.fullmatch(r'v[0-9A-Za-z.+-]+', name) and name not in allowed:
            shutil.rmtree(os.path.join(channel_dir, name))
PY"
}

dry_run() {
    normalize_version "$1"
    [[ "$VERSION" == *-* ]] || die "dry-run 使用预发布 SemVer，避免触发 main 前置条件"
    local root first second
    root="$(mktemp -d)"
    trap "rm -rf '$root'" EXIT
    first="$root/first"; second="$root/second"
    BUNDLE_DIR="$first"; prepare_bundle "$VERSION"
    mkdir -p "$second/assets"
    SOURCE_DATE_EPOCH="$(git -C "$PROJECT_ROOT" show -s --format=%ct HEAD)" \
        "$PROJECT_ROOT/scripts/build.sh" "$VERSION" "$second/assets/sslbt.zip"
    cmp "$first/assets/sslbt.zip" "$second/assets/sslbt.zip" || die "同 commit 构建字节不一致"
    python3 "$MANIFEST_TOOL" index --manifest "$first/manifest.json" --bundle-dir "$first" \
        --output "$first/release-candidate.json" --keep 5
    python3 "$MANIFEST_TOOL" check-index --index "$first/release-candidate.json" \
        --manifest "$first/manifest.json" --bundle-dir "$first"
    info "dry-run 通过：确定性构建、manifest 和候选索引均已验证（未连接发布节点）"
}

case "$MODE" in
    prepare) [ -n "$VERSION" ] || die "缺少版本号"; prepare_bundle "$VERSION" ;;
    dry-run) [ -n "$VERSION" ] || die "缺少版本号"; dry_run "$VERSION" ;;
    test-connections) test_connections ;;
    stage) [ -n "$BUNDLE_DIR" ] || die "缺少 bundle 路径"; stage_nodes false ;;
    publish) [ -n "$BUNDLE_DIR" ] || die "缺少 bundle 路径"; publish_nodes ;;
    verify) [ -n "$BUNDLE_DIR" ] || die "缺少 bundle 路径"; verify_nodes ;;
    resume)
        [ -n "$BUNDLE_DIR" ] || die "缺少 bundle 路径"
        verify_bundle; [ "$CHANNEL" = main ] || die "--resume 只用于正式发布恢复"
        require_main_tag; stage_nodes true; publish_nodes ;;
    dev)
        [ -n "$VERSION" ] || die "缺少版本号"
        normalize_version "$VERSION"; [[ "$VERSION" == *-* ]] || die "dev 发布只接受预发布 SemVer"
        prepare_bundle "$VERSION"; stage_nodes false; publish_nodes ;;
esac
