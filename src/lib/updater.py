"""插件在线更新模块"""

import os
import json
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from urllib.parse import urlparse
from urllib.request import Request, build_opener

from .api_client import _SafeHTTPHandler, _SafeHTTPSHandler, _create_ssl_context
from .net_guard import check_ssrf

MAX_RELEASES_SIZE = 256 * 1024
MAX_ZIP_SIZE = 10 * 1024 * 1024
CONNECT_TIMEOUT = 15
POST_UPDATE_TIMEOUT = 30

_ALLOWED_CHANNELS = ('main', 'dev')
_ALLOWED_HTTP_HOSTS = ('localhost', '127.0.0.1', '::1')


def _enforce_https(url):
    """spec 10.5: 升级下载必须 HTTPS，仅 loopback 允许 HTTP"""
    parsed = urlparse(url)
    if parsed.scheme == 'https':
        return
    if parsed.scheme == 'http' and parsed.hostname in _ALLOWED_HTTP_HOSTS:
        return
    raise ValueError("升级地址必须使用 HTTPS（仅 localhost 允许 HTTP）")


def _validate_channel(channel):
    """spec 10.5: 通道白名单，防止路径遍历"""
    if channel not in _ALLOWED_CHANNELS:
        raise ValueError("无效的升级通道: %s" % channel)


def _compare_prerelease(pre1, pre2):
    """按 semver 规范比较 pre-release 字段。

    按 `.` 拆段逐段比较：纯数字段走数值比较，数字段低于字母数字段，
    共同前缀相等时字段更多者更高（如 alpha < alpha.1）。
    """
    parts1 = pre1.split('.')
    parts2 = pre2.split('.')
    for a, b in zip(parts1, parts2):
        a_num, b_num = a.isdigit(), b.isdigit()
        if a_num and b_num:
            if int(a) != int(b):
                return int(a) - int(b)
        elif a_num:
            return -1
        elif b_num:
            return 1
        elif a != b:
            return -1 if a < b else 1
    return len(parts1) - len(parts2)


def compare_versions(v1, v2):
    """semver 比较，返回 <0, 0, >0。pre-release 低于同号正式版"""
    def parse(v):
        v = v.lstrip('v')
        # 剥离 build metadata（semver: +xxx 不参与排序）
        if '+' in v:
            v = v.split('+', 1)[0]
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
    return _compare_prerelease(pre1, pre2)


class Updater:
    """插件更新管理"""

    def __init__(self, plugin_dir, config_manager, logger):
        self._plugin_dir = plugin_dir
        self._config = config_manager
        self._logger = logger
        ssl_ctx = _create_ssl_context()
        self._opener = build_opener(
            _SafeHTTPHandler(),
            _SafeHTTPSHandler(context=ssl_ctx),
        )

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
        _enforce_https(url)
        ssrf_reason = check_ssrf(url)
        if ssrf_reason:
            raise ValueError("升级地址不安全: %s" % ssrf_reason)
        req = Request(url, headers={'User-Agent': 'sslbt-plugin'})
        resp = self._opener.open(req, timeout=CONNECT_TIMEOUT)
        data = resp.read(MAX_RELEASES_SIZE)
        return json.loads(data)

    # sslbt 产物文件名
    ARTIFACT_NAME = 'sslbt.zip'

    def _parse_releases(self, releases_data, channel):
        """解析 releases.json（spec 6.1: 通道做顶层 key）

        格式: {main: {latest, versions: [{version, released_at, checksums: {filename: hash}}]}, dev: ...}
        """
        current = self._get_current_version()
        ch = releases_data.get(channel, {})
        if not ch:
            return {'has_update': False, 'current_version': current}

        latest = ch.get('latest', '')
        if not latest:
            return {'has_update': False, 'current_version': current}
        if not latest.startswith('v'):
            latest = 'v' + latest

        has_update = compare_versions(current, latest) < 0

        # 在 versions 中找到 latest 对应条目，提取 checksum
        checksum = ''
        for v in ch.get('versions', []):
            ver = v.get('version', '')
            if not ver.startswith('v'):
                ver = 'v' + ver
            if ver == latest:
                checksum = v.get('checksums', {}).get(self.ARTIFACT_NAME, '')
                break

        return {
            'has_update': has_update,
            'current_version': current,
            'latest_version': latest,
            'checksum': checksum,
        }

    def check_update(self):
        cfg = self._config.get_config()
        release_url = cfg.get('release_url', '')
        if not release_url:
            return {'has_update': False, 'error': '未配置更新地址'}

        channel = cfg.get('upgrade_channel', 'main')
        _validate_channel(channel)
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
        """安全解压：符号链接拒绝、跳过 data/、路径遍历防护、权限设置、清除 __pycache__"""
        real_target = os.path.realpath(target_dir)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for info in zf.infolist():
                # spec 10.2: 符号链接防护
                if info.external_attr >> 28 == 0xA:
                    raise ValueError("ZIP 包含符号链接: %s" % info.filename)
                name = info.filename
                if name.startswith('data/') or name == 'data':
                    continue
                target_path = os.path.realpath(os.path.join(target_dir, name))
                if not target_path.startswith(real_target + os.sep) and target_path != real_target:
                    raise ValueError("unsafe path in zip: %s" % name)
                if info.is_dir():
                    os.makedirs(target_path, exist_ok=True)
                    os.chmod(target_path, 0o700)
                    continue
                parent = os.path.dirname(target_path)
                os.makedirs(parent, exist_ok=True)
                if parent != real_target:
                    os.chmod(parent, 0o700)
                with zf.open(info) as src, open(target_path, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(target_path, 0o600)

        for root, dirs, _files in os.walk(target_dir):
            for d in dirs[:]:
                if d == '__pycache__':
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)

    def do_update(self, version, release_url=None, channel=None, checksum=''):
        if release_url is None:
            cfg = self._config.get_config()
            release_url = cfg.get('release_url', '')
        if not release_url:
            raise ValueError("未配置更新地址")
        if channel is None:
            cfg = self._config.get_config()
            channel = cfg.get('upgrade_channel', 'main')
        _validate_channel(channel)

        # spec 6.3: GET {release_url}/{channel}/v{version}/{filename}
        ver = version if version.startswith('v') else 'v' + version
        url = "%s/%s/%s/%s" % (release_url.rstrip('/'), channel, ver, self.ARTIFACT_NAME)
        _enforce_https(url)
        ssrf_reason = check_ssrf(url)
        if ssrf_reason:
            raise ValueError("升级地址不安全: %s" % ssrf_reason)

        fd, tmp_path = tempfile.mkstemp(suffix='.zip')
        try:
            os.close(fd)
            self._logger.info("下载更新: %s", url)
            req = Request(url, headers={'User-Agent': 'sslbt-plugin'})
            resp = self._opener.open(req, timeout=30)
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
            cron_refresh = self._refresh_cron()
            return {'cron_refresh': cron_refresh}
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _refresh_cron(self):
        """在新进程中用刚解压的代码刷新计划任务（deploy-spec §7.3 步骤 8）

        当前升级请求已经导入旧版 lib.cron；在父进程中再次 import 只会命中
        sys.modules 缓存。子进程从插件目录加载新版模块，才能在本次升级完成迁移。
        失败仅记日志并返回状态：插件文件已经升级成功，不应因此回滚。
        """
        code = '''
import json
import sys
sys.path.insert(0, '/www/server/panel/class/')
sys.path.insert(0, %r)
from lib.cron import CronManager
result = CronManager(%r).refresh()
print(json.dumps(result, ensure_ascii=False))
sys.exit(0 if result.get('status') else 1)
''' % (self._plugin_dir, os.path.join(self._plugin_dir, 'data'))
        try:
            proc = subprocess.run(
                [sys.executable, '-c', code],
                cwd=self._plugin_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=POST_UPDATE_TIMEOUT,
            )
            output = [line for line in proc.stdout.splitlines() if line.strip()]
            if not output:
                detail = proc.stderr.strip() or '子进程无返回'
                res = {'status': False, 'message': detail}
            else:
                try:
                    res = json.loads(output[-1])
                except (TypeError, ValueError):
                    res = {'status': False, 'message': '无法解析子进程返回: %s' % output[-1]}
            if res.get('status'):
                self._logger.info("计划任务已随升级刷新: %s", res.get('message', ''))
            else:
                self._logger.warning("升级后刷新计划任务失败: %s", res.get('message', ''))
            return res
        except Exception as e:
            self._logger.warning("升级后刷新计划任务异常: %s", str(e))
            return {'status': False, 'message': str(e)}
