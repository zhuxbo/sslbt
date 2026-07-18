import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
RELEASE_SCRIPT = ROOT / 'build' / 'release.sh'


def write_executable(path, content):
    path.write_text(content)
    path.chmod(0o755)


def mock_release_env(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    write_executable(bin_dir / 'ssh', r'''#!/usr/bin/env bash
set -eu
host=""
last=""
for arg in "$@"; do
    case "$arg" in *@*) host="${arg#*@}" ;; esac
    last="$arg"
done
if [ -n "${FAIL_HOST:-}" ] && [ "$host" = "$FAIL_HOST" ] \
    && [[ "$last" == *"${FAIL_MATCH:-never-match}"* ]]; then
    exit 71
fi
if [ -n "${FAIL_AFTER_HOST:-}" ] && [ "$host" = "$FAIL_AFTER_HOST" ] \
    && [[ "$last" == *": > "*"asset.changed"* ]] && [[ "$last" == *"${FAIL_AFTER_MATCH:-never-match}"* ]]; then
    last="${last/"$FAIL_AFTER_MATCH"/false #}"
fi
if [ -n "${FAIL_CLEANUP_HOST:-}" ] && [ "$host" = "$FAIL_CLEANUP_HOST" ] \
    && [[ "$last" == rm\ -rf* ]] && [[ "$last" == *".staging/"* ]] && [[ "$last" == *".rollback/"* ]]; then
    exit 72
fi
bash -c "$last"
''')
    write_executable(bin_dir / 'scp', r'''#!/usr/bin/env bash
set -eu
previous=""
last=""
for arg in "$@"; do previous="$last"; last="$arg"; done
cp "$previous" "${last#*:}"
''')
    write_executable(bin_dir / 'sha256sum', r'''#!/usr/bin/env bash
set -eu
for path in "$@"; do
    hash=$(shasum -a 256 "$path" | cut -d' ' -f1)
    printf '%s  %s\n' "$hash" "$path"
done
''')
    real_git = shutil.which('git')
    write_executable(bin_dir / 'git', f'''#!/usr/bin/env bash
if [ "${{FORCE_CLEAN_GIT:-}}" = 1 ] && [[ "$*" == *"status --porcelain"* ]]; then
    exit 0
fi
if [ "${{FORCE_MAIN_GIT:-}}" = 1 ]; then
    case "$*" in
        *"branch --show-current"*) echo main; exit 0 ;;
        *"rev-parse origin/main"*) echo "$MOCK_HEAD_COMMIT"; exit 0 ;;
        *"show-ref --verify --quiet refs/tags/"*) exit 1 ;;
        *"rev-parse v"*"^{{commit}}"*) echo "$MOCK_HEAD_COMMIT"; exit 0 ;;
        *"ls-remote origin refs/tags/"*)
            for arg in "$@"; do
                case "$arg" in
                    refs/tags/v*"^{{}}") ;;
                    refs/tags/v*) printf '%s\t%s\n' "$MOCK_HEAD_COMMIT" "$arg"; break ;;
                esac
            done
            exit 0 ;;
    esac
fi
exec "{real_git}" "$@"
''')

    key = tmp_path / 'mock-key'
    key.write_text('not-a-real-key')
    key.chmod(0o600)
    nodes = [tmp_path / 'node1', tmp_path / 'node2']
    for node in nodes:
        node.mkdir()
    config = tmp_path / 'release.conf'
    config.write_text(
        'SERVERS=(\n'
        f'  "one,node1,22,{nodes[0]}"\n'
        f'  "two,node2,22,{nodes[1]}"\n'
        ')\n'
        'SSH_USER="release"\n'
        f'SSH_KEY="{key}"\n'
    )
    config.chmod(0o600)
    env = os.environ.copy()
    env['PATH'] = str(bin_dir) + os.pathsep + env['PATH']
    env['SSLBT_RELEASE_CONFIG'] = str(config)
    env['MOCK_HEAD_COMMIT'] = subprocess.run(
        [real_git, 'rev-parse', 'HEAD'], cwd=ROOT, text=True, capture_output=True, check=True,
    ).stdout.strip()
    return env, nodes


def run_release(args, env, check=True):
    return subprocess.run(
        ['bash', str(RELEASE_SCRIPT), *args], cwd=ROOT, env=env,
        text=True, capture_output=True, check=check,
    )


def test_dev_release_stages_then_publishes_identical_assets(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    env['FORCE_CLEAN_GIT'] = '1'
    bundle = tmp_path / 'bundle'
    run_release(['--dev', '9.8.7-rc.1', '--bundle', str(bundle)], env)

    hashes = []
    for node in nodes:
        asset = node / 'dev' / 'v9.8.7-rc.1' / 'sslbt.zip'
        hashes.append(hashlib.sha256(asset.read_bytes()).hexdigest())
        index = json.loads((node / 'releases.json').read_text())
        assert index['dev']['latest'] == '9.8.7-rc.1'
        assert index['dev']['versions'][0]['dirty'] is False
        assert not (node / 'install.sh').exists()
        assert not (node / '.publish-lock').exists()
    assert hashes[0] == hashes[1]
    assert not (bundle / '.publish-process.lock').exists()


def test_publish_failure_rolls_back_all_nodes(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    env['FORCE_CLEAN_GIT'] = '1'
    env['FORCE_MAIN_GIT'] = '1'
    baseline = {
        'main': {'latest': '', 'versions': []},
        'dev': {'latest': '1.0.0-rc.1', 'versions': [{
            'version': '1.0.0-rc.1', 'released_at': '2026-01-01',
            'checksums': {'sslbt.zip': 'sha256:' + '0' * 64},
            'source_commit': '0' * 40, 'dirty': False,
        }]},
    }
    baseline_text = json.dumps(baseline, indent=2) + '\n'
    for node in nodes:
        (node / 'releases.json').write_text(baseline_text)

    bundle = tmp_path / 'bundle'
    run_release(['--prepare', '1.0.1', '--bundle', str(bundle)], env)
    run_release(['--stage', str(bundle)], env)
    failing_env = dict(env, FAIL_HOST='node2', FAIL_MATCH='releases.json.tmp')
    result = run_release(['--publish', str(bundle)], failing_env, check=False)
    assert result.returncode != 0
    for node in nodes:
        assert (node / 'releases.json').read_text() == baseline_text
        assert not (node / 'install.sh').exists()
        assert not (node / 'main' / 'v1.0.1').exists()


def test_stage_can_resume_before_tag_without_rebuilding_candidate(tmp_path):
    env, _ = mock_release_env(tmp_path)
    bundle = tmp_path / 'bundle'
    run_release(['--prepare', '1.0.2-rc.1', '--bundle', str(bundle)], env)
    run_release(['--stage', str(bundle)], env)
    candidate = (bundle / 'release-candidate.json').read_bytes()

    run_release(['--stage', str(bundle)], env)

    assert (bundle / 'release-candidate.json').read_bytes() == candidate
    assert (bundle / 'transaction.json').exists()


def test_publish_rejects_candidate_changed_after_stage(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    bundle = tmp_path / 'bundle'
    run_release(['--prepare', '1.0.3-rc.1', '--bundle', str(bundle)], env)
    run_release(['--stage', str(bundle)], env)
    with (bundle / 'release-candidate.json').open('a') as handle:
        handle.write(' ')

    result = run_release(['--publish', str(bundle)], env, check=False)

    assert result.returncode != 0
    for node in nodes:
        assert not (node / 'dev' / 'v1.0.3-rc.1').exists()


def test_publish_rejects_index_changed_since_stage(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    bundle = tmp_path / 'bundle'
    run_release(['--prepare', '1.0.4-rc.1', '--bundle', str(bundle)], env)
    run_release(['--stage', str(bundle)], env)
    changed = {'main': {'latest': '', 'versions': []}, 'dev': {'latest': 'other', 'versions': []}}
    for node in nodes:
        (node / 'releases.json').write_text(json.dumps(changed))

    result = run_release(['--publish', str(bundle)], env, check=False)

    assert result.returncode != 0
    for node in nodes:
        assert json.loads((node / 'releases.json').read_text()) == changed
        assert not (node / 'dev' / 'v1.0.4-rc.1').exists()


def test_publish_rejects_index_read_failure(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    bundle = tmp_path / 'bundle'
    run_release(['--prepare', '1.0.4-rc.2', '--bundle', str(bundle)], env)
    run_release(['--stage', str(bundle)], env)
    failing_env = dict(env, FAIL_HOST='node1', FAIL_MATCH='then cat')

    result = run_release(['--publish', str(bundle)], failing_env, check=False)

    assert result.returncode != 0
    for node in nodes:
        assert not (node / 'dev' / 'v1.0.4-rc.2').exists()


def test_asset_partial_failure_rolls_back_current_node(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    bundle = tmp_path / 'bundle'
    run_release(['--prepare', '1.0.4-rc.3', '--bundle', str(bundle)], env)
    run_release(['--stage', str(bundle)], env)
    failing_env = dict(env, FAIL_AFTER_HOST='node2', FAIL_AFTER_MATCH="cp '")

    result = run_release(['--publish', str(bundle)], failing_env, check=False)

    assert result.returncode != 0
    for node in nodes:
        assert not (node / 'dev' / 'v1.0.4-rc.3').exists(), result.stderr


def test_old_bundle_cannot_overwrite_newer_published_index(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    old_bundle = tmp_path / 'old-bundle'
    new_bundle = tmp_path / 'new-bundle'
    run_release(['--dev', '1.0.5-rc.1', '--bundle', str(old_bundle)], env)
    run_release(['--dev', '1.0.6-rc.1', '--bundle', str(new_bundle)], env)

    result = run_release(['--publish', str(old_bundle)], env, check=False)

    assert result.returncode != 0
    for node in nodes:
        index = json.loads((node / 'releases.json').read_text())
        assert index['dev']['latest'] == '1.0.6-rc.1'
        assert (node / 'dev' / 'v1.0.6-rc.1' / 'sslbt.zip').exists()


def test_publish_does_not_reenter_same_transaction_lock(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    bundle = tmp_path / 'bundle'
    run_release(['--prepare', '1.0.7-rc.1', '--bundle', str(bundle)], env)
    run_release(['--stage', str(bundle)], env)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    transaction_id = (
        f"sslbt-v{manifest['version']}-{manifest['source_commit'][:12]}-"
        f"{manifest['assets'][0]['sha256'][:8]}-{manifest['bootstrap']['sha256'][:8]}"
    )
    for node in nodes:
        lock = node / '.publish-lock'
        lock.mkdir()
        (lock / 'owner').write_text(transaction_id + ':other-run\n')

    result = run_release(['--publish', str(bundle)], env, check=False)

    assert result.returncode != 0
    assert not (bundle / '.publish-process.lock').exists()
    for node in nodes:
        assert (node / '.publish-lock' / 'owner').read_text() == transaction_id + ':other-run\n'


def test_cleanup_interruption_resumes_forward_without_rollback_split(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    bundle = tmp_path / 'bundle'
    run_release(['--prepare', '1.0.8-rc.1', '--bundle', str(bundle)], env)
    run_release(['--stage', str(bundle)], env)
    failing_env = dict(env, FAIL_CLEANUP_HOST='node2')
    first = run_release(['--publish', str(bundle)], failing_env, check=False)
    assert first.returncode != 0

    run_release(['--publish', str(bundle)], env)

    for node in nodes:
        index = json.loads((node / 'releases.json').read_text())
        assert index['dev']['latest'] == '1.0.8-rc.1'
        assert (node / 'dev' / 'v1.0.8-rc.1' / 'sslbt.zip').exists()
        assert not (node / '.publish-lock').exists()


def test_built_plugin_reports_injected_version(tmp_path):
    archive = tmp_path / 'sslbt.zip'
    subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'build.sh'), '3.2.1-rc.4', str(archive)],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    plugin = tmp_path / 'plugin'
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
        assert {'sslbt_main.py', 'index.html', 'info.json', 'install.sh'} <= names
        assert any(name.startswith('lib/') for name in names)
        package.extractall(plugin)
    assert json.loads((plugin / 'info.json').read_text())['versions'] == '3.2.1-rc.4'
    result = subprocess.run(
        [sys.executable, str(plugin / 'sslbt_main.py'), '--version'],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == '3.2.1-rc.4'


def test_remote_installer_rejects_missing_checksum():
    content = (ROOT / 'deploy' / 'install.sh').read_text()
    assert '版本索引缺少 sslbt.zip 的 SHA256，拒绝安装' in content
    assert '跳过 SHA256 校验' not in content
    assert "cfg['upgrade_channel'] = channel" in content
