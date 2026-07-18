import hashlib
import json
import os
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
    return env, nodes


def run_release(args, env, check=True):
    return subprocess.run(
        ['bash', str(RELEASE_SCRIPT), *args], cwd=ROOT, env=env,
        text=True, capture_output=True, check=check,
    )


def test_dev_release_stages_then_publishes_identical_assets(tmp_path):
    env, nodes = mock_release_env(tmp_path)
    bundle = tmp_path / 'bundle'
    expected_dirty = bool(subprocess.run(
        ['git', 'status', '--porcelain'], cwd=ROOT, text=True,
        capture_output=True, check=True,
    ).stdout)
    run_release(['--dev', '9.8.7-rc.1', '--bundle', str(bundle)], env)

    hashes = []
    for node in nodes:
        asset = node / 'dev' / 'v9.8.7-rc.1' / 'sslbt.zip'
        hashes.append(hashlib.sha256(asset.read_bytes()).hexdigest())
        index = json.loads((node / 'releases.json').read_text())
        assert index['dev']['latest'] == '9.8.7-rc.1'
        assert index['dev']['versions'][0]['dirty'] is expected_dirty
    assert hashes[0] == hashes[1]


def test_publish_failure_rolls_back_all_nodes(tmp_path):
    env, nodes = mock_release_env(tmp_path)
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
    run_release(['--prepare', '1.0.1-rc.1', '--bundle', str(bundle)], env)
    run_release(['--stage', str(bundle)], env)
    failing_env = dict(env, FAIL_HOST='node2', FAIL_MATCH='releases.json.tmp')
    result = run_release(['--publish', str(bundle)], failing_env, check=False)
    assert result.returncode != 0
    for node in nodes:
        assert (node / 'releases.json').read_text() == baseline_text
        assert not (node / 'dev' / 'v1.0.1-rc.1').exists()


def test_built_plugin_reports_injected_version(tmp_path):
    archive = tmp_path / 'sslbt.zip'
    subprocess.run(
        ['bash', str(ROOT / 'scripts' / 'build.sh'), '3.2.1-rc.4', str(archive)],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    plugin = tmp_path / 'plugin'
    with zipfile.ZipFile(archive) as package:
        package.extractall(plugin)
    result = subprocess.run(
        [sys.executable, str(plugin / 'sslbt_main.py'), '--version'],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == '3.2.1-rc.4'


def test_remote_installer_rejects_missing_checksum():
    content = (ROOT / 'deploy' / 'install.sh').read_text()
    assert '版本索引缺少 sslbt.zip 的 SHA256，拒绝安装' in content
    assert '跳过 SHA256 校验' not in content
