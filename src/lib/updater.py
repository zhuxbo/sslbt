"""插件在线更新模块"""

import os
import json
import hashlib
import shutil
import tempfile
import zipfile
from urllib.request import Request, urlopen

MAX_RELEASES_SIZE = 256 * 1024
MAX_ZIP_SIZE = 10 * 1024 * 1024
CONNECT_TIMEOUT = 15


def compare_versions(v1, v2):
    """semver 比较，返回 <0, 0, >0。pre-release 低于同号正式版"""
    def parse(v):
        v = v.lstrip('v')
        if '-' in v:
            base, pre = v.split('-', 1)
        else:
            base, pre = v, None
        parts = [int(x) for x in base.split('.')]
        while len(parts) < 3:
            parts.append(0)
        return parts, pre

    p1, pre1 = parse(v1)
    p2, pre2 = parse(v2)

    for a, b in zip(p1, p2):
        if a != b:
            return a - b

    if pre1 is None and pre2 is None:
        return 0
    if pre1 is None:
        return 1
    if pre2 is None:
        return -1
    if pre1 < pre2:
        return -1
    if pre1 > pre2:
        return 1
    return 0


class Updater:
    """插件更新管理"""

    def __init__(self, plugin_dir, config_manager, logger):
        self._plugin_dir = plugin_dir
        self._config = config_manager
        self._logger = logger

    def _get_current_version(self):
        info_path = os.path.join(self._plugin_dir, 'info.json')
        try:
            with open(info_path, 'r', encoding='utf-8') as f:
                info = json.load(f)
            ver = info.get('versions', '0.0.0')
            if not ver.startswith('v'):
                ver = 'v' + ver
            return ver
        except (OSError, json.JSONDecodeError):
            return 'v0.0.0'

    def _fetch_releases(self, release_url):
        url = release_url.rstrip('/') + '/releases.json'
        req = Request(url, headers={'User-Agent': 'sslbt-plugin'})
        resp = urlopen(req, timeout=CONNECT_TIMEOUT)
        data = resp.read(MAX_RELEASES_SIZE)
        return json.loads(data)

    def _parse_releases(self, releases_data, channel):
        current = self._get_current_version()
        ch = releases_data.get('channels', {}).get(channel, {})
        latest = ch.get('latest', '')
        if not latest:
            return {'has_update': False, 'current_version': current}

        if not latest.startswith('v'):
            latest = 'v' + latest

        has_update = compare_versions(current, latest) < 0
        download_path = ''
        for v_entry in ch.get('versions', []):
            if v_entry.get('version') == latest:
                download_path = v_entry.get('path', '')
                break

        ver_info = releases_data.get('versions', {}).get(latest, {})
        checksum = ver_info.get('checksums', {}).get('sslbt.zip', '')

        return {
            'has_update': has_update,
            'current_version': current,
            'latest_version': latest,
            'download_path': download_path,
            'checksum': checksum,
        }

    def check_update(self):
        cfg = self._config.get_config()
        release_url = cfg.get('release_url', '')
        if not release_url:
            return {'has_update': False, 'error': '未配置更新地址'}

        channel = cfg.get('update_channel', 'main')
        try:
            releases = self._fetch_releases(release_url)
            return self._parse_releases(releases, channel)
        except Exception as e:
            self._logger.error("检查更新失败: %s", str(e))
            return {'has_update': False, 'error': str(e)}

    def _verify_checksum(self, file_path, expected):
        if not expected or not expected.startswith('sha256:'):
            self._logger.error("更新包未提供校验和，拒绝安装")
            return False
        expected_hash = expected[7:]
        h = hashlib.sha256()
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest() == expected_hash

    def _safe_extract(self, zip_path, target_dir):
        """安全解压：跳过 data/，路径遍历防护（realpath 检查），清除 __pycache__"""
        real_target = os.path.realpath(target_dir)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for info in zf.infolist():
                name = info.filename
                if name.startswith('data/') or name == 'data':
                    continue
                target_path = os.path.realpath(os.path.join(target_dir, name))
                if not target_path.startswith(real_target + os.sep) and target_path != real_target:
                    raise ValueError("unsafe path in zip: %s" % name)
                if info.is_dir():
                    os.makedirs(target_path, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with zf.open(info) as src, open(target_path, 'wb') as dst:
                    shutil.copyfileobj(src, dst)

        for root, dirs, _files in os.walk(target_dir):
            for d in dirs[:]:
                if d == '__pycache__':
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)

    def do_update(self, version, release_url=None, download_path=None, checksum=''):
        if release_url is None:
            cfg = self._config.get_config()
            release_url = cfg.get('release_url', '')
        if not release_url:
            raise ValueError("未配置更新地址")

        if download_path:
            url = "%s/%s/sslbt.zip" % (release_url.rstrip('/'), download_path)
        else:
            channel = 'dev' if '-' in version else 'main'
            url = "%s/%s/%s/sslbt.zip" % (release_url.rstrip('/'), channel, version)

        fd, tmp_path = tempfile.mkstemp(suffix='.zip')
        try:
            os.close(fd)
            self._logger.info("下载更新: %s", url)
            req = Request(url, headers={'User-Agent': 'sslbt-plugin'})
            resp = urlopen(req, timeout=30)
            with open(tmp_path, 'wb') as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    if os.path.getsize(tmp_path) > MAX_ZIP_SIZE:
                        raise ValueError("ZIP 文件超过大小限制")

            if not self._verify_checksum(tmp_path, checksum):
                raise ValueError("SHA256 校验失败")

            self._safe_extract(tmp_path, self._plugin_dir)
            self._logger.info("更新完成: %s", version)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
